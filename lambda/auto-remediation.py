import os
import time
from datetime import datetime, timedelta
import boto3

# ===========================================================
# AWS CLIENTS - used to talk to different AWS services
# ===========================================================
ec2 = boto3.client('ec2')
ssm = boto3.client('ssm')
sns = boto3.client('sns')
cloudwatch = boto3.client('cloudwatch')

# ===========================================================
# SETTINGS - read from Lambda environment variables
# ===========================================================
SNS_TOPIC_ARN = os.environ['SNS_TOPIC_ARN']

DEV_ALARM_NAME = "dev-instance-alarm"
PROD_ALARM_NAME = "prod-instance-alarm"

MEM_CRITICAL_LEVEL = 75
DISK_CRITICAL_LEVEL = 75


# ===========================================================
# STEP 1: Find out which instance triggered the alarm
# ===========================================================
def get_instance_id_from_event(event):

    try:
        metric_info = event["alarmData"]["configuration"]["metrics"][0]
        instance_id = metric_info["metricStat"]["metric"]["dimensions"]["InstanceId"]
        return instance_id

    except (KeyError, IndexError):
        print("Could not find instance ID in event")
        return None


# ===========================================================
# STEP 2: Read one CloudWatch metric's latest value.
# We use a 5 minute window (same as the alarm's period) and
# take the MOST RECENT datapoint, so this number matches what
# actually triggered the alarm - instead of averaging several
# datapoints together, which can hide the spike that caused it.
# ===========================================================
def get_metric_average(namespace, metric_name, instance_id):

    end_time = datetime.utcnow()
    start_time = end_time - timedelta(minutes=5)

    response = cloudwatch.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric_name,
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        StartTime=start_time,
        EndTime=end_time,
        Period=300,
        Statistics=["Average"]
    )

    datapoints = response["Datapoints"]

    if len(datapoints) == 0:
        return 0

    # Go through each datapoint and keep track of the newest one
    newest_time = None
    newest_value = 0

    for point in datapoints:
        if newest_time is None or point["Timestamp"] > newest_time:
            newest_time = point["Timestamp"]
            newest_value = point["Average"]

    return round(newest_value, 2)


# ===========================================================
# STEP 3: Get CPU, Memory, and Disk together for one instance
# ===========================================================
def get_instance_health(instance_id):

    cpu = get_metric_average("AWS/EC2", "CPUUtilization", instance_id)
    memory = get_metric_average("CWAgent", "mem_used_percent", instance_id)
    disk = get_metric_average("CWAgent", "disk_used_percent", instance_id)

    print("Instance:", instance_id)
    print("CPU:", cpu, "Memory:", memory, "Disk:", disk)

    return {"cpu": cpu, "memory": memory, "disk": disk}


# ===========================================================
# STEP 4: Send an email using SNS
# ===========================================================
def send_email(subject, message):

    try:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject,
            Message=message
        )
    except Exception as error:
        print("Could not send email:", error)


# ===========================================================
# Run all the diagnostic commands and get the raw output back.
# We check ONCE after a short wait - no repeated checking,
# so this stays fast.
# ===========================================================
def run_all_diagnostics(instance_id):

    commands = [
        "echo TOP PROCESSES:",
        "ps -eo comm,%cpu --sort=-%cpu --no-headers | head -3",
        "echo LOAD AVERAGE:",
        "uptime",
        "echo HTTPD STATUS:",
        "systemctl is-active httpd"
    ]

    response = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": commands}
    )
    command_id = response["Command"]["CommandId"]

    # Just one short wait, then one check - keeps this fast
    time.sleep(3)

    try:
        result = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        status = result["Status"]

        if status == "Success":
            return result.get("StandardOutputContent", "No output returned")

        return f"Command status was '{status}'. Check SSM Run Command console for full output."

    except ssm.exceptions.InvocationDoesNotExist:
        return "Output not ready yet. Check SSM Run Command console for full output."


# ===========================================================
# DEV ACTION - Stop the instance
# ===========================================================
def stop_dev_instance(instance_id, health):

    try:
        ec2.stop_instances(InstanceIds=[instance_id])
        print("Stop command sent for dev instance", instance_id)

        # Verify - wait a moment, then check the instance's state
        time.sleep(3)
        response = ec2.describe_instances(InstanceIds=[instance_id])
        current_state = response["Reservations"][0]["Instances"][0]["State"]["Name"]
        print("Instance state after stop command:", current_state)

        send_email(
            "Dev Instance Stopped",
            f"Stop command sent for {instance_id}.\n"
            f"Current state: {current_state}\n"
            f"CPU: {health['cpu']}%, Memory: {health['memory']}%, Disk: {health['disk']}%"
        )

    except Exception as error:
        print("Failed to stop dev instance:", error)
        send_email(
            "Dev Instance Stop Failed",
            f"Could not stop {instance_id}. Error: {error}"
        )


# ===========================================================
# PROD ACTION 1 - Disk is critical, clean up old files
# ===========================================================
def clean_up_disk_space(instance_id, health):

    try:
        response = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={
                "commands": [
                    "echo '===== DISK BEFORE CLEANUP ====='",
                    "df -h /",

                    "echo '===== CLEANING JOURNAL LOGS ====='",
                    "sudo journalctl --vacuum-time=2d",

                    "echo '===== DELETING OLD ROTATED LOGS ====='",
                    "sudo find /var/log -type f -name '*.log.*' -mtime +7 -delete",

                    "echo '===== CLEANING TEMP FILES ====='",
                    "sudo find /tmp -mindepth 1 -delete",

                    "echo '===== DISK AFTER CLEANUP ====='",
                    "df -h /"
                ]
            }
        )

        command_id = response["Command"]["CommandId"]

        print("Disk cleanup command sent to", instance_id)
        print("Command ID:", command_id)

        # Wait for SSM command to complete
        time.sleep(5)

        result = ssm.get_command_invocation(
            CommandId=command_id,
            InstanceId=instance_id
        )

        status = result["Status"]

        print("Disk cleanup command status:", status)

        # Print SSM command output
        stdout = result.get("StandardOutputContent", "")
        stderr = result.get("StandardErrorContent", "")

        print("===== CLEANUP OUTPUT =====")
        print(stdout)

        if stderr:
            print("===== CLEANUP ERRORS =====")
            print(stderr)

        send_email(
            "Disk Cleanup Triggered",
            f"Disk usage was critical on {instance_id}.\n\n"
            f"Command status: {status}\n"
            f"Disk usage before cleanup: {health['disk']}%\n\n"
            f"Cleanup output:\n{stdout}\n\n"
            f"Errors:\n{stderr}"
        )

    except Exception as error:

        print("Failed to run disk cleanup:", error)

        send_email(
            "Disk Cleanup Failed",
            f"Could not clean up disk on {instance_id}.\n"
            f"Error: {error}"
        )


# ===========================================================
# PROD ACTION 2 - Memory is critical, reboot the instance
# ===========================================================
def reboot_instance(instance_id, health):

    try:
        ec2.reboot_instances(InstanceIds=[instance_id])
        print("Reboot command sent for", instance_id)

        # Verify - wait a moment, then check the instance's state
        time.sleep(3)
        response = ec2.describe_instances(InstanceIds=[instance_id])
        current_state = response["Reservations"][0]["Instances"][0]["State"]["Name"]
        print("Instance state after reboot command:", current_state)

        send_email(
            "Production Instance Rebooted",
            f"Memory usage was critical, so {instance_id} was rebooted.\n"
            f"Current state: {current_state}\n"
            f"CPU: {health['cpu']}%, Memory: {health['memory']}%, Disk: {health['disk']}%"
        )

    except Exception as error:
        print("Failed to reboot instance:", error)
        send_email(
            "Production Reboot Failed",
            f"Could not reboot {instance_id}. Error: {error}"
        )


# ===========================================================
# PROD ACTION 3 - Only CPU is high, collect real diagnostics
# and email the actual output, formatted simply.
# ===========================================================
def collect_diagnostics(instance_id, health):

    print("Collecting diagnostics for", instance_id)

    diagnostic_output = run_all_diagnostics(instance_id)

    message = (
        f"High CPU detected\n"
        f"Instance: {instance_id}\n"
        f"CPU: {health['cpu']}%\n"
        f"Memory: {health['memory']}%\n"
        f"Disk: {health['disk']}%\n\n"
        f"{diagnostic_output}"
    )

    send_email("High CPU Detected", message)


# ===========================================================
# MAIN FUNCTION - AWS calls this when an alarm fires
# ===========================================================
def lambda_handler(event, context):

    print("Lambda started")
    print("Event:", event)

    alarm_name = event.get("alarmData", {}).get("alarmName")
    instance_id = get_instance_id_from_event(event)

    print("Alarm name:", alarm_name)
    print("Instance ID:", instance_id)

    if not alarm_name or not instance_id:
        print("Missing alarm name or instance ID, stopping here")
        return {"statusCode": 400, "message": "Missing alarm name or instance ID"}

    if alarm_name == DEV_ALARM_NAME:

        health = get_instance_health(instance_id)
        stop_dev_instance(instance_id, health)

    elif alarm_name == PROD_ALARM_NAME:

        health = get_instance_health(instance_id)

        if health["disk"] >= DISK_CRITICAL_LEVEL:
            print("Disk is critical - cleaning up disk space")
            clean_up_disk_space(instance_id, health)

        elif health["memory"] >= MEM_CRITICAL_LEVEL:
            print("Memory is critical - rebooting instance")
            reboot_instance(instance_id, health)

        else:
            print("Only CPU is high - collecting diagnostics")
            collect_diagnostics(instance_id, health)

    else:
        print("No matching alarm for:", alarm_name)

    return {
        "statusCode": 200,
        "message": "Lambda finished running"
    }