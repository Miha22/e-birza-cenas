import os
import re
import time
from datetime import datetime
import pytz
import requests
from bs4 import BeautifulSoup
import paho.mqtt.client as mqtt
from config import TRUSTED_CLIENTS_FILE, MASTER_PUBLIC_KEY_HEX, SUBS_COOLDOWN
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.exceptions import InvalidSignature
from fastapi import FastAPI, HTTPException, status, Body, Header
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.exceptions import InvalidSignature

try:
    master_public_key = ed25519.Ed25519PublicKey.from_public_bytes(
        bytes.fromhex(MASTER_PUBLIC_KEY_HEX)
    )
except Exception as e:
    print(f"Critical Error loading Master Public Key: {e}")
    master_public_key = None

app = FastAPI(title="Electricity Price API")
TRUSTED_CLIENTS = set()

def load_trusted_clients_into_memory():
    TRUSTED_CLIENTS.clear()
    if TRUSTED_CLIENTS_FILE.exists():
        with open(TRUSTED_CLIENTS_FILE, "r") as f:
            for line in f:
                clean_key = line.strip()
                if clean_key:
                    TRUSTED_CLIENTS.add(clean_key)

MQTT_BROKER = "m3.wqtt.ru"
MQTT_PORT = 13855
MQTT_USER = "dima"
MQTT_PASSWORD = "Telephone"
MQTT_TOPIC_PRICE = "birza/electricity_price"
MQTT_TOPIC_TIME = "birza/electricity_price/time"

URL = "https://www.e-cena.lv"
LOCAL_TIMEZONE = pytz.timezone('Europe/Riga')

cache = {
    "last_scraped_at": 0,
    "price": None,
    "time_str": None
}

def get_local_time():
    return datetime.now(LOCAL_TIMEZONE)

def scrape_and_publish():
    try:
        response = requests.get(URL, timeout=10)
        response.encoding = 'utf-8'
        if response.status_code != 200:
            return None, None

        soup = BeautifulSoup(response.text, 'html.parser')
        now = get_local_time()
        current_hour = now.hour
        current_minute = now.minute

        if current_minute < 15:
            time_str = f"{current_hour:02d}:00"
        elif current_minute < 30:
            time_str = f"{current_hour:02d}:15"
        elif current_minute < 45:
            time_str = f"{current_hour:02d}:30"
        else:
            time_str = f"{current_hour:02d}:45"
            if current_hour == 23 and current_minute >= 45:
                time_str = "23:45"

        price_found = None
        rows = soup.find_all('tr')
        for row in rows:
            cells = row.find_all('td')
            if len(cells) >= 2:
                price_text = cells[0].get_text().strip()
                time_text = cells[1].get_text().strip()
                if time_text == time_str:
                    try:
                        price_found = float(price_text.replace(',', '.'))
                        break
                    except ValueError:
                        continue

        if price_found is None:
            all_text = soup.get_text()
            lines = all_text.split('\n')
            for i, line in enumerate(lines):
                if time_str in line and i+1 < len(lines):
                    numbers = re.findall(r'\d+\.\d+', lines[i+1])
                    if numbers:
                        price_found = float(numbers[0].replace(',', '.'))
                        break

        if price_found is not None:
            try:
                client = mqtt.Client()
                client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
                client.connect(MQTT_BROKER, MQTT_PORT, 60)
                
                formatted_price = f"{price_found:.5f}"
                formatted_time = now.strftime("%Y-%m-%d %H:%M:%S")
                
                client.publish(MQTT_TOPIC_PRICE, formatted_price, qos=1, retain=True)
                client.publish(MQTT_TOPIC_TIME, formatted_time, qos=1, retain=True)
                client.disconnect()
            except Exception as mqtt_err:
                print(f"MQTT Error ignored during cache refresh: {mqtt_err}")

            return price_found, time_str

    except Exception as e:
        print(f"Scraping Error: {e}")
        
    return None, None

load_trusted_clients_into_memory()

@app.get("/api/price")
def get_price(
    x_client_id: str = Header(None),
    x_timestamp: str = Header(None),
    x_signature: str = Header(None)
):

    if not all([x_client_id, x_timestamp, x_signature]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing security credentials in headers (X-Client-ID, X-Timestamp, or X-Signature)."
        )

    if x_client_id not in TRUSTED_CLIENTS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access Denied: Public key is not whitelisted."
        )

    try:
        request_time = float(x_timestamp)
        current_time = time.time()
        if abs(current_time - request_time) > 30:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Request expired. Timestamp variance too high (possible replay attack)."
            )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Malformed timestamp header format."
        )

    try:
        public_key_bytes = bytes.fromhex(x_client_id)
        public_key = ed25519.Ed25519PublicKey.from_public_bytes(public_key_bytes)
        signature_bytes = bytes.fromhex(x_signature)
        public_key.verify(signature_bytes, x_timestamp.encode())
    except InvalidSignature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Cryptographic signature verification failed."
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Error processing cryptographic parameters."
        )

    time_elapsed = current_time - cache["last_scraped_at"]

    cooldown_limit = SUBS_COOLDOWN
    if cooldown_limit < 1:
        cooldown_limit = 300
        print(f"Setting cooldown limit to default value {cooldown_limit} sec.\n")

    if time_elapsed < cooldown_limit and cache["price"] is not None:
        remaining_cooldown = int(cooldown_limit - time_elapsed)
        return {
            "status": "cached",
            "cooldown_active": True,
            "seconds_until_refresh_allowed": remaining_cooldown,
            "price": cache["price"],
            "target_time": cache["time_str"]
        }

    fresh_price, fresh_time = scrape_and_publish()

    if fresh_price is not None:
        cache["price"] = fresh_price
        cache["time_str"] = fresh_time
        cache["last_scraped_at"] = current_time
        
        return {
            "status": "fresh",
            "cooldown_active": False,
            "seconds_until_refresh_allowed": 300,
            "price": fresh_price,
            "target_time": fresh_time
        }
    else:
        if cache["price"] is not None:
            return {
                "status": "scraping_failed_serving_stale_cache",
                "price": cache["price"],
                "target_time": cache["time_str"]
            }
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, 
            detail="Scraping target failed and no data is currently cached."
        )
    
@app.post("/api/register", status_code=status.HTTP_201_CREATED)
def register_client(
    new_client_public_key: str = Body(..., embed=True),
    signature: str = Body(..., embed=True),
    timestamp: str = Body(..., embed=True)
):
    if not master_public_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server configuration error: Master key is missing."
        )

    try:
        request_time = float(timestamp)
        if abs(time.time() - request_time) > 45:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Registration link expired. Check system time synchronization."
            )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed timestamp format."
        )

    verification_payload = f"{new_client_public_key}:{timestamp}".encode()
    try:
        signature_bytes = bytes.fromhex(signature)
        master_public_key.verify(signature_bytes, verification_payload)
    except InvalidSignature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Master Key authorization signature is invalid."
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed cryptographic payload parameters."
        )

    clean_new_key = new_client_public_key.strip()

    if clean_new_key in TRUSTED_CLIENTS:
        return {
            "status": "exists", 
            "message": "This client public key is already whitelisted and active."
        }

    try:
        with open(TRUSTED_CLIENTS_FILE, "a") as f:
            f.write(f"{clean_new_key}\n")
        
        TRUSTED_CLIENTS.add(clean_new_key)
        
        return {
            "status": "success",
            "message": "Client public key verified and whitelisted successfully."
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to persist key write to storage: {e}"
        )