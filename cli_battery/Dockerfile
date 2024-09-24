# Use an official Python runtime as a parent image
FROM python:3.11

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the current directory contents into the container at /app
COPY . /app

# Create necessary directories and files
RUN mkdir -p logs

# Ensure Python output is sent straight to terminal without buffering
ENV PYTHONUNBUFFERED=1

# Make ports available for gRPC and Flask
EXPOSE 50051 5001

# Run the main.py file when the container launches
CMD ["python", "/app/main.py"]