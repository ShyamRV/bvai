from twilio.rest import Client
import os

client = Client(
    os.environ["TWILIO_ACCOUNT_SID"],
    os.environ["TWILIO_AUTH_TOKEN"]
)

call = client.calls.create(
    to="+918431439772",  # ← replace with YOUR phone number
    from_="+18885039433",
    url="https://bvai-production.up.railway.app/voice/inbound",
    method="POST"
)

print("Call triggered:", call.sid)
