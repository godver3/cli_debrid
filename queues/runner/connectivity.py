"""Service connectivity checks and what happens when a service goes down.

Methods of ProgramRunner (queues/run_program.py), kept here to keep that file manageable.
"""
import logging
import time


class ConnectivityTasks:

    def task_check_service_connectivity(self):
        """Check connectivity to required services"""
        from routes.program_operation_routes import check_service_connectivity

        # Log current state before checking
        if self.connectivity_failure_time:
            time_since_failure = time.time() - self.connectivity_failure_time
            logging.info(f"[Connectivity Check Task] Running scheduled check. Previous failure detected {time_since_failure:.0f}s ago")
        else:
            logging.debug("[Connectivity Check Task] Running routine connectivity check")
            
        connectivity_ok, failed_services = check_service_connectivity()
        
        if connectivity_ok:
            if self.connectivity_failure_time:
                # We're recovering from a previous failure
                logging.info("[Connectivity Check Task] Service connectivity RESTORED via scheduled task")
                self.connectivity_failure_time = None
                self.connectivity_retry_count = 0
                self.pause_info = {
                    "reason_string": None, "error_type": None, "service_name": None,
                    "status_code": None, "retry_count": 0
                }
                self.resume_queue()
            else:
                logging.debug("[Connectivity Check Task] Service connectivity check passed")
        else:
            if self.connectivity_failure_time:
                # Already in failure state, just log
                logging.warning(f"[Connectivity Check Task] Services still unavailable: {[s.get('service') for s in failed_services]}")
            else:
                # New failure detected
                logging.error(f"[Connectivity Check Task] Service connectivity check failed: {[s.get('service') for s in failed_services]}")
                self.handle_connectivity_failure(failed_services)

    def handle_connectivity_failure(self, failed_services_details=None): # MODIFIED: expects detailed list

        current_pause_info = {
            "reason_string": "Connectivity failure - waiting for services to be available",
            "error_type": "CONNECTION_ERROR", # Generic default
            "service_name": "Multiple Services", # Generic default
            "status_code": None,
            "retry_count": 0 # Initial failure
        }

        if failed_services_details and len(failed_services_details) > 0:
            # Construct a detailed reason string
            reason_parts = []
            primary_error_set = False
            for detail in failed_services_details:
                service = detail.get("service", "Unknown Service")
                message = detail.get("message", "unavailable")
                reason_parts.append(f"{service}: {message}")

                # Set the primary error based on the first critical issue found
                # Prioritize Debrid Unauthorized/Forbidden
                if not primary_error_set:
                    error_type = detail.get("type", "CONNECTION_ERROR")
                    if "Debrid" in service and error_type in ["UNAUTHORIZED", "FORBIDDEN"]:
                        current_pause_info["error_type"] = error_type
                        current_pause_info["status_code"] = detail.get("status_code")
                        current_pause_info["service_name"] = service
                        primary_error_set = True
                    elif not primary_error_set and error_type == "CONNECTION_ERROR": # Catch first connection error
                        current_pause_info["error_type"] = "CONNECTION_ERROR"
                        current_pause_info["status_code"] = detail.get("status_code")
                        current_pause_info["service_name"] = service
                        # Don't set primary_error_set = True here, to allow critical errors to override

            current_pause_info["reason_string"] = "Connectivity failure - " + "; ".join(reason_parts)
            if not primary_error_set and failed_services_details: # If no specific critical error, use the first service
                 current_pause_info["service_name"] = failed_services_details[0].get("service", "Unknown Service")


        logging.warning(f"Pausing program queue due to connectivity failure: {current_pause_info['reason_string']}")
        # Update self.pause_info
        self.pause_info = current_pause_info
        self.pause_queue() 
        
        if not self.connectivity_failure_time:
            self.connectivity_failure_time = time.time()
            self.connectivity_retry_count = 0 # Reset legacy retry counter

    def check_connectivity_status(self):
        from routes.program_operation_routes import check_service_connectivity

        if not self.connectivity_failure_time:
            return 
            
        # Use the legacy self.connectivity_retry_count for retry timing logic
        time_since_failure = time.time() - self.connectivity_failure_time
        
        # Check every 30 seconds for first 2 minutes, then every 60 seconds
        if time_since_failure <= 120:  # First 2 minutes
            retry_interval = 30
        else:
            retry_interval = 60
            
        # Calculate if it's time for next retry
        time_since_last_retry = time_since_failure - (self.connectivity_retry_count * retry_interval)
        
        if time_since_last_retry >= retry_interval:
            self.connectivity_retry_count += 1 # Increment legacy counter for timing
            
            logging.info(f"Checking service connectivity (attempt {self.connectivity_retry_count}, {time_since_failure:.0f}s since failure)")
            
            try:
                connectivity_ok, failed_services_details = check_service_connectivity()
                if connectivity_ok:
                    logging.info("Service connectivity restored")
                    self.connectivity_failure_time = None
                    self.connectivity_retry_count = 0
                    # Clear pause_info on resume
                    self.pause_info = {
                        "reason_string": None, "error_type": None, "service_name": None,
                        "status_code": None, "retry_count": 0
                    }
                    self.resume_queue()
                    return
            except Exception as e:
                logging.error(f"Error checking service connectivity: {str(e)}")
            
            logging.warning(f"Service connectivity check failed. Overall retry attempt {self.connectivity_retry_count}")

            # Update self.pause_info with new details
            updated_pause_info = {
                "reason_string": f"Connectivity failure - waiting for services (Retry {self.connectivity_retry_count})",
                "error_type": "CONNECTION_ERROR",
                "service_name": "Multiple Services",
                "status_code": None,
                "retry_count": self.connectivity_retry_count
            }

            if failed_services_details and len(failed_services_details) > 0:
                reason_parts = []
                primary_error_set = False
                for detail in failed_services_details:
                    service = detail.get("service", "Unknown Service")
                    message = detail.get("message", "unavailable")
                    reason_parts.append(f"{service}: {message}")

                    if not primary_error_set:
                        error_type = detail.get("type", "CONNECTION_ERROR")
                        if "Debrid" in service and error_type in ["UNAUTHORIZED", "FORBIDDEN"]:
                            updated_pause_info["error_type"] = error_type
                            updated_pause_info["status_code"] = detail.get("status_code")
                            updated_pause_info["service_name"] = service
                            primary_error_set = True
                        elif not primary_error_set and error_type == "CONNECTION_ERROR":
                             updated_pause_info["error_type"] = "CONNECTION_ERROR"
                             updated_pause_info["status_code"] = detail.get("status_code")
                             updated_pause_info["service_name"] = service
                
                updated_pause_info["reason_string"] = f"Connectivity failure - {'; '.join(reason_parts)} (Retry {self.connectivity_retry_count})"
                if not primary_error_set and failed_services_details:
                    updated_pause_info["service_name"] = failed_services_details[0].get("service", "Unknown Service")

            self.pause_info = updated_pause_info
