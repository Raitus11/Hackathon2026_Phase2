from tachyon_langchain_client import TachyonLangchainClient
from dotenv import load_dotenv

import requests
import os
import httpx
import uuid
import base64

load_dotenv(override=True)
httpx_client = httpx.Client(verify=False)

# Tachyon Configuration
USECASE_ID = os.environ.get("USE_CASE_ID")
API_KEY = os.environ.get("API_KEY")
CLIENT_ID = os.environ.get("CLIENT_ID")
MODEL = os.environ.get("MODEL")
BASE_URL = os.environ.get("BASE_URL")
APIGEE_URL = os.environ.get("APIGEE_URL")
CONSUMER_KEY = os.environ.get("CONSUMER_KEY")
CONSUMER_SECRET = os.environ.get("CONSUMER_SECRET")
CERTS_PATH = os.environ.get("CERTS_PATH")

print(f"USECASE_ID: {USECASE_ID}")
print(f"API_KEY: {API_KEY}")
print(f"CLIENT_ID: {CLIENT_ID}")
print(f"MODEL: {MODEL}")
print(f"BASE_URL: {BASE_URL}")
print(f"APIGEE_URL: {APIGEE_URL}")
print(f"CONSUMER_KEY: {CONSUMER_KEY}")
print(f"CONSUMER_SECRET: {CONSUMER_SECRET}")
print(f"CERTS_PATH: {CERTS_PATH}")

llm = TachyonLangchainClient(model_name="gemini-2.0-flash-001", temperature=0.1)

messages = "What is today's date?"

ai_msg = llm.invoke(messages)
print(f"\n*****\nResponse: {ai_msg.content}*****\n")
