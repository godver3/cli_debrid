# Use Python 3 as the base image
FROM python:3.11-alpine

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app/

# Install any needed packages specified in requirements.txt
RUN pip install -r requirements.txt

# Create necessary directories and files
RUN mkdir -p logs db_content config && \
    touch logs/debug.log logs/info.log logs/queue.log

# Make the entrypoint script executable
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Set the TERM environment variable for proper terminal attachment
ENV TERM=xterm

# Comment out unwanted commands in shell initialization files
RUN sed -i 's/^export LC_ALL=C.UTF-8/# export LC_ALL=C.UTF-8/' /etc/profile && \
    sed -i 's/^clear/# clear/' /etc/profile

# Make port 5000 available to the world outside this container
EXPOSE 5000

# Set the entrypoint
ENTRYPOINT ["/entrypoint.sh"]
