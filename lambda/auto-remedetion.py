import boto3

# AWS Clients
ec2 = boto3.client('ec2')
ssm = boto3.client('ssm')

# EC2 Instance IDs
DEV_INSTANCE = "i-01d0915e85840f533"
PROD_INSTANCE = "i-0ab7ec5044ce24627"


def lambda_handler(event, context):

    print("Lambda Started")
    print(event)

    # Get alarm name
    alarm_name = event.get("alarmData", {}).get("alarmName")

    print("Alarm Name:", alarm_name)

    # DEV Alarm - Stop Instance
    if alarm_name == "projecct_proactive":

        print("Stopping DEV Instance")

        ec2.stop_instances(
            InstanceIds=[DEV_INSTANCE]
        )

        print("DEV Instance Stop Command Sent")

    # PROD Auto Remediation - Restart Apache
    elif alarm_name == "prod-restart-private":

        print("Restarting Apache on PROD Server")

        ssm.send_command(
            InstanceIds=[PROD_INSTANCE],
            DocumentName="AWS-RunShellScript",
            Parameters={
                "commands": [
                    "sudo systemctl restart httpd"
                ]
            }
        )

        print("Apache Restart Command Sent")

    # PROD Reboot
    elif alarm_name == "prod_auto_remedation":

        print("Rebooting Production Instance")

        ec2.reboot_instances(
            InstanceIds=[PROD_INSTANCE]
        )

        print("Reboot Command Sent")

    else:

        print("No Matching Alarm Found")

    return {
        "statusCode": 200,
        "message": "Lambda Code Executed Successfully"
    }