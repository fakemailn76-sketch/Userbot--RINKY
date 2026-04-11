# Python 3.11 slim বেস
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# সিস্টেম বেসিকস (টাইমজোন/সার্ট), ইমেজ ছোট রাখতে --no-install-recommends
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

# সোর্স কপি
COPY . /app

# ডিপেন্ডেন্সি ইনস্টল (requirements.txt ছাড়াই)
RUN pip install --no-cache-dir telethon==1.36.0 PySocks==1.7.1

# non-root ইউজার
RUN useradd -m appuser
USER appuser

# বট চালু
CMD ["python", "-u", "bot.py"]