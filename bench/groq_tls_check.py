import requests
import certifi

print("certifi bundle:", certifi.where())
for host in ("https://api.groq.com/openai/v1/models", "https://generativelanguage.googleapis.com/", "https://www.csisoftware.com/"):
    try:
        r = requests.get(host, timeout=15)  # normal verification, no auth header
        print(host, "-> HTTP", r.status_code, "(TLS verified)")
    except requests.exceptions.SSLError as e:
        msg = str(e)
        i = msg.find("SSL:")
        print(host, "-> TLS VERIFY FAILED:", msg[i:i + 110] if i >= 0 else msg[:150])
    except Exception as e:
        print(host, "->", type(e).__name__)
