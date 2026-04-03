FROM python:3.11-slim
 
ENV PYTHONUNBUFFERED=1

# Create a non-root user
RUN useradd -m -u 1000 sentinel

WORKDIR /app

# Copy dependencies and install
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy everything
COPY . /app/
RUN chown -R sentinel:sentinel /app

# Switch to non-root user
USER sentinel

# By default, we run the script in daemon mode
CMD ["python3", "seerr_sentinel.py", "daemon", "--interval", "60"]
