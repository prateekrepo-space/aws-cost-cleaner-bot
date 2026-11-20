# Automated AWS Cost-Cleaner Bot - Container Image for AWS ECS Fargate
FROM python:3.12-slim

# Prevent Python from writing .pyc files and enable unbuffered logging for CloudWatch
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Create unprivileged system user and group for container security compliance
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /bin/sh -d /app appuser

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/ src/

# Set ownership to unprivileged user
RUN chown -R appuser:appgroup /app

# Run as non-root user
USER appuser

# Entrypoint executes the cost cleaner job and terminates upon completion
CMD ["python", "-u", "src/main.py"]
