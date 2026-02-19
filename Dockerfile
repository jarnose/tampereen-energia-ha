# Use the official Playwright image with dependencies pre-installed
FROM mcr.microsoft.com/playwright/python:v1.41.0-jammy

# Set the working directory
WORKDIR /app

# Copy requirements and install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the python script
COPY main.py .

# Run the python script
CMD ["python", "main.py"]
