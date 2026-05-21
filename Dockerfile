FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Install Python dependencies (cache-friendly: deps layer separate from code)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright browsers are pre-installed in the base image
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Create data directories
RUN mkdir -p data/pdfs

COPY . .

CMD ["python", "main.py"]
