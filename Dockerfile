# 1. Use the official Playwright image
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# 2. Set the working directory
WORKDIR /app

# 3. Copy requirements first (to cache dependencies)
COPY requirements.txt .

# 4. Install Python dependencies (As root, so permissions are easy)
RUN pip install --no-cache-dir -r requirements.txt

# 5. Install Chrome
RUN playwright install chromium

# 6. Copy the rest of the app
COPY . .

# 7. ⚠️ CRITICAL FIX: 
# The user "1000" already exists in this image. We just need to 
# give them ownership of the /app folder so they can run the script.
RUN chown -R 1000:1000 /app

# 8. Switch to the existing user (UID 1000) for security
USER 1000

# 9. Expose the port
EXPOSE 7860

# 10. Start the app
CMD ["python", "app.py"]