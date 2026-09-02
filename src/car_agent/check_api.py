import os

from anthropic import Anthropic
from dotenv import load_dotenv


# 读取项目根目录下的 .env
load_dotenv()

api_key = os.getenv("ANTHROPIC_API_KEY")
base_url = os.getenv("ANTHROPIC_BASE_URL")
model = os.getenv("MODEL_ID")

# 只检查是否读取成功，不打印 API Key
print(f"API Key loaded: {bool(api_key)}")
print(f"Base URL: {base_url}")
print(f"Model: {model}")

client = Anthropic(
    api_key=api_key,
    base_url=base_url,
)

response = client.messages.create(
    model=model,
    max_tokens=100,
    messages=[
        {
            "role": "user",
            "content": "你好，请只回复：API连接成功",
        }
    ],
)

print("\n模型返回：")

for block in response.content:
    if block.type == "text":
        print(block.text)