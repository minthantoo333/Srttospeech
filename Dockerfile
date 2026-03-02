# Use official python slim image
FROM python:3.10-slim

# Install ffmpeg for pydub to process audio
RUN apt-get update && apt-get install -y ffmpeg

# Set the working directory
WORKDIR /app

# Copy dependency list and install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the app's code
COPY . .

# Run the bot
CMD ["python", "bot.py"]
