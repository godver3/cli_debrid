# Use Python 3 as the base image
FROM python:3.11-alpine

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app/

# Install any needed packages specified in requirements.txt
RUN pip install -r requirements.txt

# Make the entrypoint script executable
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Set the TERM environment variable for proper terminal attachment
ENV TERM=xterm

# Make port 5000 available to the world outside this container
EXPOSE 5000

# Set the entrypoint
ENTRYPOINT ["/entrypoint.sh"]
