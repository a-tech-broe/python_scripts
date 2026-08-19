#!/usr/bin/envbash

###############################################################################
#EnterpriseECS/FargateHealthCheck
#
#Purpose:
#Read-onlyholisticdiagnosticassessmentofanECS/Fargatecluster.
#
#Checks:
#-AWSauthentication/account
#-ECScluster
#-ECSservices
#-Desired/running/pendingtaskcounts
#-Deployments
#-Deploymentcircuitbreaker
#-ECSserviceevents
#-Runningtasks
#-Containerhealth
#-Taskconnectivity
#-Stoppedtasks
#-Stopcodes/reasons
#-Taskdefinitions
#-CPU/memoryconfiguration
#-Healthchecks
#-Environment/loggingconfiguration
#-ALBtargetgroups
#-Targethealth
#-Networkconfiguration
#-CloudWatchloggroups
#-Servicescalingconfiguration
#
#IMPORTANT:
#ThisscriptisREADONLY.
#Itdoesnotrestartservices,stoptasks,modifydeployments,
#modifysecuritygroups,orchangeAWSresources.
###############################################################################

set-uopipefail

###############################################################################
#DEFAULTS
###############################################################################

ACCOUNT=""
CLUSTER=""
REGION=""
PROFILE=""
OUTPUT_DIR="./ecs-health-$(date'+%Y%m%d-%H%M%S')"

FAILURES=0
WARNINGS=0

###############################################################################
#COLORS
###############################################################################

if[[-t1]];then
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
BOLD='\033[1m'
NC='\033[0m'
else
RED=''
GREEN=''
YELLOW=''
BLUE=''
CYAN=''
MAGENTA=''
BOLD=''
NC=''
fi

###############################################################################
#FUNCTIONS
###############################################################################

usage(){
cat<<EOF

EnterpriseECS/FargateHealthCheck

Usage:

$0--accountACCOUNT_ID--clusterCLUSTER_NAME--regionREGION[OPTIONS]

Required:

--accountAWSAccountID
--clusterECSClusterName
--regionAWSRegion

Optional:

--profileAWSCLIprofile
--outputOutputdirectory
--helpShowhelp

Examples:

$0\\
--account123456789012\\
--clusterproduction\\
--regionus-east-1

$0\\
--account123456789012\\
--clusterproduction\\
--regionus-east-1\\
--profileprod

EOF
}

log(){
echo-e"$1"
}

section(){
echo
echo"======================================================================"
echo-e"${BOLD}${BLUE}$1${NC}"
echo"======================================================================"
}

subsection(){
echo
echo-e"${BOLD}${CYAN}$1${NC}"
echo"----------------------------------------------------------------------"
}

pass(){
echo-e"${GREEN}[PASS]${NC}$1"
}

warn(){
echo-e"${YELLOW}[WARN]${NC}$1"
WARNINGS=$((WARNINGS+1))
}

fail(){
echo-e"${RED}[FAIL]${NC}$1"
FAILURES=$((FAILURES+1))
}

info(){
echo-e"${CYAN}[INFO]${NC}$1"
}

###############################################################################
#ARGUMENTPARSING
###############################################################################

while[[$#-gt0]];do

case"$1"in

--account)
ACCOUNT="$2"
shift2
;;

--cluster)
CLUSTER="$2"
shift2
;;

--region)
REGION="$2"
shift2
;;

--profile)
PROFILE="$2"
shift2
;;

--output)
OUTPUT_DIR="$2"
shift2
;;

--help|-h)
usage
exit0
;;

*)
echo"Unknownargument:$1"
usage
exit1
;;

esac

done

###############################################################################
#VALIDATION
###############################################################################

if[[-z"$ACCOUNT"||-z"$CLUSTER"||-z"$REGION"]];then
echo"ERROR:--account,--clusterand--regionarerequired."
usage
exit1
fi

if![["$ACCOUNT"=~^[0-9]{12}$]];then
echo"ERROR:AWSaccountIDmustbea12-digitnumber."
exit1
fi

mkdir-p"$OUTPUT_DIR"

###############################################################################
#AWSCLIBASECOMMAND
###############################################################################

AWS_CMD=(aws)

if[[-n"$PROFILE"]];then
AWS_CMD+=(--profile"$PROFILE")
fi

AWS_CMD+=(--region"$REGION"--no-cli-pager)

aws_cmd(){
"${AWS_CMD[@]}""$@"
}

###############################################################################
#COMMANDDEPENDENCYCHECK
###############################################################################

section"PRE-FLIGHTCHECK"

if!command-vaws>/dev/null2>&1;then
fail"AWSCLIisnotinstalled."
exit1
fi

if!command-vjq>/dev/null2>&1;then
fail"jqisrequired."
echo
echo"Install:"
echo"macOS:brewinstalljq"
echo"Ubuntu:sudoapt-getinstalljq"
exit1
fi

AWS_VERSION=$(aws--version2>&1)

info"AWSCLI:$AWS_VERSION"
info"Region:$REGION"
info"Cluster:$CLUSTER"
info"Accountrequested:$ACCOUNT"
info"Output:$OUTPUT_DIR"

###############################################################################
#AWSIDENTITY
###############################################################################

section"AWSACCOUNT/IDENTITY"

IDENTITY=$(aws_cmdstsget-caller-identity2>&1)

if[[$?-ne0]];then
fail"UnabletoauthenticatetoAWS."
echo"$IDENTITY"
exit1
fi

CURRENT_ACCOUNT=$(echo"$IDENTITY"|jq-r'.Account')
CALLER_ARN=$(echo"$IDENTITY"|jq-r'.Arn')

echo"$IDENTITY">"$OUTPUT_DIR/aws-identity.json"

info"Caller:$CALLER_ARN"

if[["$CURRENT_ACCOUNT"!="$ACCOUNT"]];then
fail"ACCOUNTMISMATCH!"
echo
echo"Requestedaccount:$ACCOUNT"
echo"Authenticated:$CURRENT_ACCOUNT"
echo
echo"Refusingtocontinue."
exit1
fi

pass"AuthenticatedtoexpectedAWSaccount:$ACCOUNT"

###############################################################################
#ECSCLUSTER
###############################################################################

section"ECSCLUSTER"

CLUSTER_JSON=$(aws_cmdecsdescribe-clusters\
--clusters"$CLUSTER"\
--includeATTACHMENTSCONFIGURATIONSSETTINGSSTATISTICSTAGS2>&1)

echo"$CLUSTER_JSON">"$OUTPUT_DIR/cluster.json"

ifecho"$CLUSTER_JSON"|jq-e'.failures|length>0'>/dev/null2>&1;then
fail"ECSclustercouldnotbedescribed."
echo"$CLUSTER_JSON"|jq'.failures'
exit1
fi

CLUSTER_STATUS=$(echo"$CLUSTER_JSON"|jq-r'.clusters[0].status//"UNKNOWN"')
CLUSTER_ARN=$(echo"$CLUSTER_JSON"|jq-r'.clusters[0].clusterArn//"UNKNOWN"')
RUNNING_TASKS=$(echo"$CLUSTER_JSON"|jq-r'.clusters[0].runningTasksCount//0')
PENDING_TASKS=$(echo"$CLUSTER_JSON"|jq-r'.clusters[0].pendingTasksCount//0')
ACTIVE_SERVICES=$(echo"$CLUSTER_JSON"|jq-r'.clusters[0].activeServicesCount//0')

echo
echo"ClusterARN:$CLUSTER_ARN"
echo"Clusterstatus:$CLUSTER_STATUS"
echo"Runningtasks:$RUNNING_TASKS"
echo"Pendingtasks:$PENDING_TASKS"
echo"Activeservices:$ACTIVE_SERVICES"

if[["$CLUSTER_STATUS"!="ACTIVE"]];then
fail"ClusterisnotACTIVE."
else
pass"ClusterisACTIVE."
fi

if[["$PENDING_TASKS"-gt0]];then
warn"Clusterhas$PENDING_TASKSpendingtask(s)."
else
pass"Nopendingtasks."
fi

###############################################################################
#LISTSERVICES
###############################################################################

section"ECSSERVICES"

SERVICE_ARNS=$(aws_cmdecslist-services\
--cluster"$CLUSTER"\
--outputtext\
--query'serviceArns[]'2>/dev/null)

if[[-z"$SERVICE_ARNS"]];then
warn"NoECSservicesfoundincluster."
exit0
fi

SERVICE_COUNT=$(echo"$SERVICE_ARNS"|wc-w|tr-d'')

info"Servicesdiscovered:$SERVICE_COUNT"

###############################################################################
#SERVICELOOP
###############################################################################

SERVICE_INDEX=0

forSERVICE_ARNin$SERVICE_ARNS;do

SERVICE_INDEX=$((SERVICE_INDEX+1))

SERVICE_NAME="${SERVICE_ARN##*/}"

subsection"SERVICE$SERVICE_INDEX/$SERVICE_COUNT:$SERVICE_NAME"

SERVICE_JSON=$(aws_cmdecsdescribe-services\
--cluster"$CLUSTER"\
--services"$SERVICE_NAME"\
--includeTAGS2>/dev/null)

echo"$SERVICE_JSON">"$OUTPUT_DIR/service-${SERVICE_NAME}.json"

SERVICE=$(echo"$SERVICE_JSON"|jq'.services[0]')

if[["$SERVICE"=="null"]];then
fail"Unabletoretrieveservice$SERVICE_NAME."
continue
fi

STATUS=$(echo"$SERVICE"|jq-r'.status//"UNKNOWN"')
DESIRED=$(echo"$SERVICE"|jq-r'.desiredCount//0')
RUNNING=$(echo"$SERVICE"|jq-r'.runningCount//0')
PENDING=$(echo"$SERVICE"|jq-r'.pendingCount//0')
LAUNCH_TYPE=$(echo"$SERVICE"|jq-r'.launchType//"CAPACITY_PROVIDER"')
TASK_DEFINITION=$(echo"$SERVICE"|jq-r'.taskDefinition//"UNKNOWN"')
PLATFORM=$(echo"$SERVICE"|jq-r'.platformVersion//"N/A"')
DEPLOYMENT_TYPE=$(echo"$SERVICE"|jq-r'.deploymentController.type//"ECS"')

echo
echo"Status:$STATUS"
echo"Launchtype:$LAUNCH_TYPE"
echo"Deploymenttype:$DEPLOYMENT_TYPE"
echo"Desiredtasks:$DESIRED"
echo"Runningtasks:$RUNNING"
echo"Pendingtasks:$PENDING"
echo"Platformversion:$PLATFORM"
echo"Taskdefinition:$TASK_DEFINITION"

###########################################################################
#SERVICESTATUS
###########################################################################

if[["$STATUS"=="ACTIVE"]];then
pass"ServiceisACTIVE."
else
fail"Servicestatusis$STATUS."
fi

###########################################################################
#DESIREDVSRUNNING
###########################################################################

if[["$DESIRED"-eq"$RUNNING"]];then
pass"Desiredandrunningtaskcountsmatch."
else
fail"Desired/runningmismatch:desired=$DESIREDrunning=$RUNNING"
fi

if[["$PENDING"-gt0]];then
warn"$PENDINGtask(s)currentlypending."
fi

###########################################################################
#DEPLOYMENTS
###########################################################################

subsection"DEPLOYMENTS"

DEPLOYMENTS=$(echo"$SERVICE"|jq-c'.deployments[]?')

DEPLOYMENT_COUNT=$(echo"$SERVICE"|jq'.deployments|length')

echo"Deploymentcount:$DEPLOYMENT_COUNT"

whileread-rDEPLOYMENT;do

[[-z"$DEPLOYMENT"]]&&continue

D_STATUS=$(echo"$DEPLOYMENT"|jq-r'.status//"UNKNOWN"')
D_RUNNING=$(echo"$DEPLOYMENT"|jq-r'.runningCount//0')
D_DESIRED=$(echo"$DEPLOYMENT"|jq-r'.desiredCount//0')
D_ROLLOUT=$(echo"$DEPLOYMENT"|jq-r'.rolloutState//"UNKNOWN"')
D_REASON=$(echo"$DEPLOYMENT"|jq-r'.rolloutStateReason//""')

echo
echo"Status:$D_STATUS"
echo"Rolloutstate:$D_ROLLOUT"
echo"Running:$D_RUNNING"
echo"Desired:$D_DESIRED"

if[[-n"$D_REASON"]];then
echo"Reason:$D_REASON"
fi

if[["$D_ROLLOUT"=="FAILED"]];then
fail"DeploymentrolloutFAILED."
elif[["$D_ROLLOUT"=="IN_PROGRESS"]];then
warn"DeploymentrolloutisIN_PROGRESS."
elif[["$D_ROLLOUT"=="COMPLETED"]];then
pass"Deploymentrolloutcompleted."
fi

done<<<"$DEPLOYMENTS"

###########################################################################
#DEPLOYMENTCIRCUITBREAKER
###########################################################################

subsection"DEPLOYMENTCIRCUITBREAKER"

CIRCUIT_BREAKER=$(echo"$SERVICE"|jq-r'.deploymentConfiguration.deploymentCircuitBreaker//empty')

if[[-n"$CIRCUIT_BREAKER"]];then

CB_ENABLED=$(echo"$SERVICE"|jq-r'.deploymentConfiguration.deploymentCircuitBreaker.enable//false')
CB_ROLLBACK=$(echo"$SERVICE"|jq-r'.deploymentConfiguration.deploymentCircuitBreaker.rollback//false')

echo"Enabled:$CB_ENABLED"
echo"Rollback:$CB_ROLLBACK"

if[["$CB_ENABLED"=="true"]];then
pass"Deploymentcircuitbreakerenabled."
else
warn"Deploymentcircuitbreakerdisabled."
fi

else
warn"Nodeploymentcircuitbreakerconfigurationdetected."
fi

###########################################################################
#SERVICEEVENTS
###########################################################################

subsection"RECENTSERVICEEVENTS"

EVENTS=$(echo"$SERVICE"|jq-r'
.events[0:10][]?|
"\(.createdAt)|\(.message)"
')

if[[-n"$EVENTS"]];then
echo"$EVENTS"

ifecho"$EVENTS"|grep-Eiq\
"unable|failed|failure|cannot|cannotpull|unhealthy|timeout|unabletoplace|stopped|draining|insufficient";then

warn"Potentiallyunhealthyserviceeventsdetected."

else
pass"Noobviousfailurekeywordsdetectedinrecentserviceevents."
fi

else
info"Noserviceeventsreturned."
fi

###########################################################################
#TASKDEFINITION
###########################################################################

subsection"TASKDEFINITION"

TD_JSON=$(aws_cmdecsdescribe-task-definition\
--task-definition"$TASK_DEFINITION"\
--includeTAGS2>/dev/null)

echo"$TD_JSON">"$OUTPUT_DIR/task-definition-${SERVICE_NAME}.json"

TD=$(echo"$TD_JSON"|jq'.taskDefinition')

if[["$TD"=="null"]];then

fail"Unabletoretrievetaskdefinition."

else

TD_CPU=$(echo"$TD"|jq-r'.cpu//"N/A"')
TD_MEMORY=$(echo"$TD"|jq-r'.memory//"N/A"')
TD_NETWORK=$(echo"$TD"|jq-r'.networkMode//"N/A"')
TD_REVISION=$(echo"$TD"|jq-r'.revision//"N/A"')
TD_STATUS=$(echo"$TD"|jq-r'.status//"N/A"')

echo"Revision:$TD_REVISION"
echo"Status:$TD_STATUS"
echo"CPU:$TD_CPU"
echo"Memory:$TD_MEMORY"
echo"Networkmode:$TD_NETWORK"

#######################################################################
#CONTAINERDEFINITIONS
#######################################################################

echo
echo"Containers:"

echo"$TD"|jq-r'
.containerDefinitions[]|
"-\(.name)
image:\(.image)
essential:\(.essential)
cpu:\(.cpu//0)
memory:\(.memory//"N/A")
memoryReservation:\(.memoryReservation//"N/A")
healthCheck:\(if.healthCheckthen"configured"else"NOTCONFIGURED"end)
logging:\(.logConfiguration.logDriver//"NONE")"
'

#######################################################################
#HEALTHCHECK
#######################################################################

HEALTHLESS=$(echo"$TD"|jq'
[.containerDefinitions[]|
select(.essential==trueand(.healthCheck==null))]
|length
')

if[["$HEALTHLESS"-gt0]];then
warn"$HEALTHLESSessentialcontainer(s)havenoECScontainerhealthcheck."
else
pass"Essentialcontainershavehealthchecksconfigured."
fi

fi

###########################################################################
#RUNNINGTASKS
###########################################################################

subsection"RUNNINGTASKS"

RUNNING_TASK_ARNS=$(aws_cmdecslist-tasks\
--cluster"$CLUSTER"\
--service-name"$SERVICE_NAME"\
--desired-statusRUNNING\
--launch-typeFARGATE\
--query'taskArns[]'\
--outputtext2>/dev/null)

if[[-z"$RUNNING_TASK_ARNS"]];then

fail"NoRUNNINGFargatetasksfound."

else

TASK_JSON=$(aws_cmdecsdescribe-tasks\
--cluster"$CLUSTER"\
--tasks$RUNNING_TASK_ARNS\
--includeTAGS2>/dev/null)

echo"$TASK_JSON">"$OUTPUT_DIR/running-tasks-${SERVICE_NAME}.json"

echo"$TASK_JSON"|jq-r'
.tasks[]|
"Task:\(.taskArn|split("/")[-1])
Status:\(.lastStatus)
Desired:\(.desiredStatus)
Health:\(.healthStatus//"UNKNOWN")
Connectivity:\(.connectivity//"UNKNOWN")
AZ:\(.availabilityZone//"N/A")
Platform:\(.platformVersion//"N/A")
CPU:\(.cpu//"N/A")
Memory:\(.memory//"N/A")
StoppedReason:\(.stoppedReason//"N/A")
"
'

#######################################################################
#CONNECTIVITY
#######################################################################

BAD_CONNECTIVITY=$(echo"$TASK_JSON"|jq'
[.tasks[]|select(.connectivity!="CONNECTED")]
|length
')

if[["$BAD_CONNECTIVITY"-gt0]];then
fail"$BAD_CONNECTIVITYrunningtask(s)donothaveCONNECTEDECSconnectivity."
else
pass"AllrunningtasksreportCONNECTED."
fi

#######################################################################
#TASKHEALTH
#######################################################################

UNHEALTHY=$(echo"$TASK_JSON"|jq'
[.tasks[]|
select(.healthStatus=="UNHEALTHY")]
|length
')

if[["$UNHEALTHY"-gt0]];then
fail"$UNHEALTHYrunningtask(s)reportUNHEALTHY."
else
pass"NorunningtasksreportUNHEALTHY."
fi

#######################################################################
#CONTAINERHEALTH
#######################################################################

BAD_CONTAINERS=$(echo"$TASK_JSON"|jq'
[.tasks[].containers[]?|
select(.healthStatus=="UNHEALTHY")]
|length
')

if[["$BAD_CONTAINERS"-gt0]];then
fail"$BAD_CONTAINERScontainer(s)reportUNHEALTHY."
else
pass"NocontainersreportUNHEALTHY."
fi

fi

###########################################################################
#STOPPEDTASKS
###########################################################################

subsection"RECENTSTOPPEDTASKS"

STOPPED_TASK_ARNS=$(aws_cmdecslist-tasks\
--cluster"$CLUSTER"\
--service-name"$SERVICE_NAME"\
--desired-statusSTOPPED\
--launch-typeFARGATE\
--query'taskArns[]'\
--outputtext2>/dev/null)

if[[-z"$STOPPED_TASK_ARNS"]];then

pass"NorecentlystoppedFargatetasksfound."

else

STOPPED_JSON=$(aws_cmdecsdescribe-tasks\
--cluster"$CLUSTER"\
--tasks$STOPPED_TASK_ARNS\
2>/dev/null)

echo"$STOPPED_JSON">"$OUTPUT_DIR/stopped-tasks-${SERVICE_NAME}.json"

STOPPED_COUNT=$(echo"$STOPPED_JSON"|jq'.tasks|length')

warn"$STOPPED_COUNTrecentlystoppedtask(s)found."

echo"$STOPPED_JSON"|jq-r'
.tasks[]|
"Task:\(.taskArn|split("/")[-1])
Stopcode:\(.stopCode//"N/A")
Stoppedreason:\(.stoppedReason//"N/A")
Stoppedat:\(.stoppedAt//"N/A")
Containers:
\(
[.containers[]?|
"\(.name):exit=\(.exitCode//"N/A")reason=\(.reason//"N/A")"]
|join("\n")
)
"
'

fi

###########################################################################
#LOADBALANCERS
###########################################################################

subsection"LOADBALANCER/TARGETGROUPS"

LB_COUNT=$(echo"$SERVICE"|jq'.loadBalancers|length')

if[["$LB_COUNT"-eq0]];then

info"Noloadbalancerconfiguredforthisservice."

else

echo"$SERVICE"|jq-r'.loadBalancers[]?.targetGroupArn'|
whileread-rTG_ARN;do

[[-z"$TG_ARN"]]&&continue

echo
echo"TargetGroup:"
echo"$TG_ARN"

TG_HEALTH=$(aws_cmdelbv2describe-target-health\
--target-group-arn"$TG_ARN"2>/dev/null)

TG_NAME=$(aws_cmdelbv2describe-target-groups\
--target-group-arns"$TG_ARN"\
--query'TargetGroups[0].TargetGroupName'\
--outputtext2>/dev/null)

echo"TargetGroupName:$TG_NAME"

echo"$TG_HEALTH"|
jq-r'
.TargetHealthDescriptions[]?|
"Target:\(.Target.Id):\(.Target.Port)
State:\(.TargetHealth.State)
Reason:\(.TargetHealth.Reason//"N/A")
Description:\(.TargetHealth.Description//"N/A")
"
'

UNHEALTHY_TARGETS=$(echo"$TG_HEALTH"|jq'
[.TargetHealthDescriptions[]?|
select(.TargetHealth.State!="healthy")]
|length
')

if[["$UNHEALTHY_TARGETS"-gt0]];then
fail"$UNHEALTHY_TARGETStarget(s)arenothealthy."
else
pass"Allregisteredtargetsarehealthy."
fi

done

fi

###########################################################################
#NETWORKCONFIGURATION
###########################################################################

subsection"NETWORKCONFIGURATION"

echo"$SERVICE"|jq-r'
.networkConfiguration.awsvpcConfiguration//empty|
"Subnets:
\(.subnets[]?//"N/A")

SecurityGroups:
\(.securityGroups[]?//"N/A")

AssignPublicIP:
\(.assignPublicIp//"N/A")
"
'

###########################################################################
#SERVICEAUTOSCALING
###########################################################################

subsection"SERVICEAUTOSCALING"

RESOURCE_ID="service/$CLUSTER/$SERVICE_NAME"

SCALING_JSON=$(aws_cmdapplication-autoscalingdescribe-scalable-targets\
--service-namespaceecs\
--resource-ids"$RESOURCE_ID"\
2>/dev/null)

echo"$SCALING_JSON">"$OUTPUT_DIR/scaling-${SERVICE_NAME}.json"

SCALABLE=$(echo"$SCALING_JSON"|jq'.ScalableTargets|length')

if[["$SCALABLE"-eq0]];then

info"NoApplicationAutoScalingtargetdetected."

else

echo"$SCALING_JSON"|jq-r'
.ScalableTargets[]|
"Mincapacity:\(.MinCapacity)
Maxcapacity:\(.MaxCapacity)
Currentdesired:\(.SuspendedState//"N/A")
"
'

POLICIES=$(aws_cmdapplication-autoscalingdescribe-scaling-policies\
--service-namespaceecs\
--resource-id"$RESOURCE_ID"\
2>/dev/null)

echo"$POLICIES">"$OUTPUT_DIR/scaling-policies-${SERVICE_NAME}.json"

POLICY_COUNT=$(echo"$POLICIES"|jq'.ScalingPolicies|length')

echo"Scalingpolicies:$POLICY_COUNT"

if[["$POLICY_COUNT"-eq0]];then
warn"Scalabletargetexistsbutnoscalingpolicieswerereturned."
else
pass"AutoScalingpoliciesdetected."
fi

fi

###########################################################################
#LOGGINGCONFIGURATION
###########################################################################

subsection"CLOUDWATCHLOGGING"

LOG_DRIVERS=$(echo"$TD"|jq-r'
.containerDefinitions[]|
"\(.name)|\(.logConfiguration.logDriver//"NONE")|\(.logConfiguration.options["awslogs-group"]//"N/A")"
')

if[[-n"$LOG_DRIVERS"]];then

whileIFS='|'read-rCONTAINERDRIVERLOG_GROUP;do

echo
echo"Container:$CONTAINER"
echo"Driver:$DRIVER"
echo"Loggroup:$LOG_GROUP"

if[["$DRIVER"=="awslogs"&&"$LOG_GROUP"!="N/A"]];then

ifaws_cmdlogsdescribe-log-groups\
--log-group-name-prefix"$LOG_GROUP"\
--query"logGroups[?logGroupName=='$LOG_GROUP'].logGroupName"\
--outputtext2>/dev/null|
grep-q"$LOG_GROUP";then

pass"CloudWatchloggroupexists."

else

fail"CloudWatchloggroupnotfound:$LOG_GROUP"

fi

fi

done<<<"$LOG_DRIVERS"

else

warn"Nocontainerloggingconfigurationdetected."

fi

done

###############################################################################
#CLUSTER-WIDESTOPPEDTASKS
###############################################################################

section"CLUSTER-WIDESTOPPEDTASKANALYSIS"

ALL_STOPPED=$(aws_cmdecslist-tasks\
--cluster"$CLUSTER"\
--desired-statusSTOPPED\
--launch-typeFARGATE\
--query'taskArns[]'\
--outputtext2>/dev/null)

if[[-n"$ALL_STOPPED"]];then

STOPPED_DETAILS=$(aws_cmdecsdescribe-tasks\
--cluster"$CLUSTER"\
--tasks$ALL_STOPPED\
2>/dev/null)

echo"$STOPPED_DETAILS">"$OUTPUT_DIR/all-stopped-tasks.json"

echo"$STOPPED_DETAILS"|jq-r'
.tasks[]|
[
(.stopCode//"UNKNOWN"),
(.stoppedReason//"UNKNOWN")
]|
@tsv
'|sort|uniq-c|sort-nr

else

pass"NorecentlystoppedFargatetasksfoundcluster-wide."

fi

###############################################################################
#CLUSTEREVENT/ERRORSCAN
###############################################################################

section"ERROR/WARNINGSCAN"

ERROR_PATTERNS='failed|failure|unhealthy|timeout|cannot|unable|insufficient|oom|outofmemory|resourceinitialization|cannotpull|imagepull|healthcheck|connectionrefused|throttl'

FOUND_ERRORS=0

forFILEin"$OUTPUT_DIR"/service-*.json;do

[[-f"$FILE"]]||continue

MATCHES=$(grep-Eio"$ERROR_PATTERNS""$FILE"2>/dev/null|sort|uniq-c)

if[[-n"$MATCHES"]];then

echo
echo"File:$(basename"$FILE")"
echo"$MATCHES"

FOUND_ERRORS=1

fi

done

if[["$FOUND_ERRORS"-eq0]];then
pass"Noobviouserrorkeywordsfoundincollectedservicedata."
else
warn"Potentialerrorindicatorsfound.ReviewserviceJSONandevents."
fi

###############################################################################
#SUMMARY
###############################################################################

section"FINALHEALTHSUMMARY"

echo
echo"AWSAccount:$CURRENT_ACCOUNT"
echo"Cluster:$CLUSTER"
echo"Region:$REGION"
echo"ClusterStatus:$CLUSTER_STATUS"
echo"Services:$SERVICE_COUNT"
echo"RunningTasks:$RUNNING_TASKS"
echo"PendingTasks:$PENDING_TASKS"
echo

echo"Healthchecks:"
echo"FAILURES:$FAILURES"
echo"WARNINGS:$WARNINGS"

echo

if[["$FAILURES"-eq0&&"$WARNINGS"-eq0]];then

echo-e"${GREEN}${BOLD}"
echo"======================================================================"
echo"HEALTHY"
echo"======================================================================"
echo-e"${NC}"

elif[["$FAILURES"-eq0]];then

echo-e"${YELLOW}${BOLD}"
echo"======================================================================"
echo"HEALTHYWITHWARNINGS"
echo"======================================================================"
echo-e"${NC}"

else

echo-e"${RED}${BOLD}"
echo"======================================================================"
echo"UNHEALTHY"
echo"======================================================================"
echo-e"${NC}"

fi

echo
echo"Diagnosticartifacts:"
echo"$OUTPUT_DIR"
echo

###############################################################################
#CREATESUMMARYFILE
###############################################################################

cat>"$OUTPUT_DIR/summary.txt"<<EOF
ECS/FargateHealthCheck
========================

Account:$CURRENT_ACCOUNT
Cluster:$CLUSTER
Region:$REGION

Clusterstatus:$CLUSTER_STATUS
Services:$SERVICE_COUNT
Runningtasks:$RUNNING_TASKS
Pendingtasks:$PENDING_TASKS

Failures:$FAILURES
Warnings:$WARNINGS

Generated:$(date-u'+%Y-%m-%dT%H:%M:%SZ')
EOF

###############################################################################
#EXITSTATUS
###############################################################################

if[["$FAILURES"-gt0]];then
exit2
elif[["$WARNINGS"-gt0]];then
exit1
else
exit0
fi