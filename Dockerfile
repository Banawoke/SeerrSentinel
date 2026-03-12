FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copy dependencies and install
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy everything
COPY . /app/

# By default, we run the script in daemon mode
CMD ["python3", "seerr_sentinel.py", "daemon", "--interval", "60"]
