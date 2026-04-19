# VenueIQ NGINX API Gateway
FROM nginx:alpine

# Copy custom NGINX template
COPY default.conf.template /etc/nginx/templates/default.conf.template

# Copy frontend static files
COPY index.html /usr/share/nginx/html/index.html
COPY venueiq-digital-twin.html /usr/share/nginx/html/venueiq-digital-twin.html

# Ensure files are readable by nginx worker process
RUN chmod -R 755 /usr/share/nginx/html

# Expose port (Cloud Run sets the PORT env var)
ENV PORT 8080
EXPOSE 8080
