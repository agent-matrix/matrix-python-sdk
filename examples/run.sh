#!/bin/bash

# A script to find and run all other .sh files, displaying logs in the terminal
# and saving them to a log file simultaneously.

# Define the log file name
LOG_FILE="run_all.log"

# Get the absolute path of the current script to ensure it's not run again.
# This prevents an infinite loop.
CURRENT_SCRIPT=$(realpath "$0")

# Clear the log file from previous runs
> "$LOG_FILE"

echo "Starting the execution of all .sh files. Logs are being displayed and saved to '$LOG_FILE'." | tee -a "$LOG_FILE"
echo "-----------------------------------------------------------------------" | tee -a "$LOG_FILE"

# Find all .sh files and process them
find . -type f -name "*.sh" -print0 | while IFS= read -r -d '' script
do
  # Get the absolute path of the found script
  SCRIPT_PATH=$(realpath "$script")

  # Check to make sure we don't run the current script itself
  if [[ "$SCRIPT_PATH" != "$CURRENT_SCRIPT" ]]
  then
    echo "▶️ Running '$script'..." | tee -a "$LOG_FILE"
    echo "-----------------------------------------------------------------------" | tee -a "$LOG_FILE"
    
    # Execute the script and use 'tee' to send output to both stdout and the log file
    # The '2>&1' part redirects stderr to stdout so it also gets logged
    bash "$script" 2>&1 | tee -a "$LOG_FILE"
    
    echo "✔️ '$script' finished." | tee -a "$LOG_FILE"
    echo "-----------------------------------------------------------------------" | tee -a "$LOG_FILE"
  fi
done

echo "✅ All scripts have been executed." | tee -a "$LOG_FILE"