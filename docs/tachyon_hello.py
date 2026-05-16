from dotenv import load_dotenv
load_dotenv()

from tachyon_langchain_client import TachyonLangchainClient

client = TachyonLangchainClient(model_name="gemini-2.5-pro")
print("client built, calling...")

resp = client.invoke([{"role": "user", "content": "Say hello in one word."}])
print("RESULT:", resp.content)
