# Use the official Playwright image with dependencies pre-installed
FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

# Set the working directory inside the container
WORKDIR /app

# Create a directory for logs
RUN mkdir -p /app/logs

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the scraper script
COPY main.py .

# Run the script
CMD ["python", "-u", "main.py"]
