import requests

session = requests.Session()
session.trust_env = False

url = "http://100.123.148.6:8080/v1/chat/completions"

payload = {
    "model": "qwen3",
    "messages": [
        {
            "role": "user",
            "content": (
                "Viết một tiểu thuyết dài nhất bạn có thể viết "
            )
        }
    ],
    "temperature": 0.2,
    "max_tokens": 20000
}

r = session.post(
    url,
    json=payload,
    timeout=120
)

r.raise_for_status()

data = r.json()

print(data["choices"][0]["message"]["content"])