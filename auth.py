import os
import requests
import dotenv

dotenv.load_dotenv()
ACCESS_TOKEN_URL = os.environ["ACCESS_TOKEN_URL"]
CLIENT_ID = os.environ["CLIENT_ID"]
CLIENT_SECRET = os.environ["CLIENT_SECRET"]
SCOPE = os.environ["SCOPE"]

def gen_token() -> str:
    try:
        response = requests.post(
            ACCESS_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "scope": SCOPE
            },
            auth=(CLIENT_ID, CLIENT_SECRET),
            timeout=10
        )
        response.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Failed to generate token: {e}")

    token = response.json()["access_token"]
    return token
