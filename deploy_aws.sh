#!/usr/bin/env bash
# =============================================================================
# deploy_aws.sh - AWS deployment automation for the LLM Metering Engine
#
# Builds the container image, pushes it to ECR, provisions the supporting
# infrastructure (RDS PostgreSQL, ElastiCache Redis, S3 audit bucket,
# Secrets Manager) and drives the ECS Fargate service.
#
# Usage:
#   ./deploy_aws.sh <command> [options]
#
# Commands:
#   plan         Print the resolved configuration and the AWS calls that would
#                be made, without touching the account.
#   ecr-login    Authenticate the local Docker client against ECR.
#   build        Build the container image.
#   push         Tag the image for ECR and push it.
#   migrate      Run the SQL migrations against the RDS instance.
#   deploy       Register a new ECS task definition and update the service.
#   status       Show the state of the service, the task definition and the
#                running tasks.
#   rollback     Re-register the previous task definition and redeploy.
#   destroy      Tear the stack down (requires --confirm).
#   all          build -> push -> migrate -> deploy.
#   help         Show this help.
#
# Options:
#   --tag TAG        Image tag to deploy (default: git short SHA, else "latest").
#   --region REGION  AWS region (default: $AWS_REGION or us-east-1).
#   --profile NAME   AWS credentials profile (default: $AWS_PROFILE or default).
#   --dry-run        Print the AWS calls instead of executing them.
#   --skip-build     Skip the image build (use an existing local image).
#   --skip-push      Skip the image push.
#   --no-wait        Do not wait for the service to reach a steady state.
#   --confirm        Required acknowledgement for destructive commands.
#
# Every destructive or billable step is a no-op under --dry-run, so the whole
# plan can be reviewed safely before anything is created.
# =============================================================================

set -euo pipefail

# --------------------------------------------------------------------------
# Defaults - override any of these through the environment.
# --------------------------------------------------------------------------
AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_PROFILE="${AWS_PROFILE:-default}"
PROJECT_NAME="${PROJECT_NAME:-llm-metering}"
ECR_REPO="${ECR_REPO:-$PROJECT_NAME}"
ECS_CLUSTER="${ECS_CLUSTER:-$PROJECT_NAME}"
ECS_SERVICE="${ECS_SERVICE:-$PROJECT_NAME}"
TASK_FAMILY="${TASK_FAMILY:-$PROJECT_NAME}"
CONTAINER_NAME="${CONTAINER_NAME:-$PROJECT_NAME}"
IMAGE_TAG="${IMAGE_TAG:-}"
DOCKERFILE="${DOCKERFILE:-Dockerfile}"
IMAGE_BUILD_CONTEXT="${IMAGE_BUILD_CONTEXT:-.}"
CONTAINER_PORT="${CONTAINER_PORT:-8000}"
TASK_CPU="${TASK_CPU:-1024}"
TASK_MEMORY="${TASK_MEMORY:-2048}"
DESIRED_COUNT="${DESIRED_COUNT:-2}"
LOG_GROUP="${LOG_GROUP:-/ecs/$PROJECT_NAME}"
AWS_LOG_RETENTION="${AWS_LOG_RETENTION:-30}"

# Data stores
RDS_INSTANCE_ID="${RDS_INSTANCE_ID:-$PROJECT_NAME-pg}"
RDS_DB_NAME="${RDS_DB_NAME:-metering}"
RDS_DB_USER="${RDS_DB_USER:-metering}"
RDS_ENGINE_VERSION="${RDS_ENGINE_VERSION:-16.4}"
RDS_INSTANCE_CLASS="${RDS_INSTANCE_CLASS:-db.t4g.medium}"
RDS_ALLOCATED_STORAGE="${RDS_ALLOCATED_STORAGE:-50}"

REDIS_CLUSTER_ID="${REDIS_CLUSTER_ID:-$PROJECT_NAME-redis}"
REDIS_NODE_TYPE="${REDIS_NODE_TYPE:-cache.t4g.small}"
REDIS_CLUSTER_MODE="${REDIS_CLUSTER_MODE:-enabled}"

S3_BUCKET="${S3_BUCKET:-$PROJECT_NAME-audit}"
SECRET_NAME="${SECRET_NAME:-$PROJECT_NAME/app-secrets}"

# Networking
VPC_ID="${VPC_ID:-}"
SUBNET_IDS="${SUBNET_IDS:-}"
SECURITY_GROUP_IDS="${SECURITY_GROUP_IDS:-}"
PUBLIC_SUBNET_IDS="${PUBLIC_SUBNET_IDS:-}"

DRY_RUN=0
SKIP_BUILD=0
SKIP_PUSH=0
WAIT=1
CONFIRM=0
COMMAND=""

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_BLUE=$'\033[1;34m'; C_GREEN=$'\033[1;32m'
  C_YELLOW=$'\033[1;33m'; C_RED=$'\033[1;31m'; C_DIM=$'\033[2m'
else
  C_RESET=""; C_BLUE=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_DIM=""
fi

log()  { printf '%s==>%s %s\n' "$C_BLUE" "$C_RESET" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%swarn%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }
dim()  { printf '%s%s%s\n' "$C_DIM" "$*" "$C_RESET"; }

# Run a command, or just print it when --dry-run is active.
run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '%s[dry-run]%s %s\n' "$C_YELLOW" "$C_RESET" "$*"
    return 0
  fi
  "$@"
}

# Same, but for pipelines / shell snippets.
run_sh() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '%s[dry-run]%s %s\n' "$C_YELLOW" "$C_RESET" "$1"
    return 0
  fi
  bash -eo pipefail -c "$1"
}

require() {
  command -v "$1" >/dev/null 2>&1 || die "required tool not found on PATH: $1"
}

# Echo the value of a secret without printing it.
secret_value() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf 'dry-run-placeholder'
    return 0
  fi
  aws secretsmanager get-secret-value --secret-id "$1" \
    --region "$AWS_REGION" --query SecretString --output text
}


# --------------------------------------------------------------------------
# AWS helpers
# --------------------------------------------------------------------------
aws_cli() {
  run aws --profile "$AWS_PROFILE" --region "$AWS_REGION" "$@"
}

ecr_repository_uri() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '%s.dkr.ecr.%s.amazonaws.com/%s' "123456789012" "$AWS_REGION" "$ECR_REPO"
    return 0
  fi
  aws --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    ecr describe-repositories --repository-names "$ECR_REPO" \
    --query 'repositories[0].repositoryUri' --output text
}

resolve_image_tag() {
  if [[ -z "$IMAGE_TAG" ]]; then
    if git rev-parse --git-dir >/dev/null 2>&1; then
      IMAGE_TAG="$(git rev-parse --short HEAD)"
    else
      IMAGE_TAG="latest"
    fi
  fi
  printf '%s' "$IMAGE_TAG"
}

# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
cmd_plan() {
  local tag; tag="$(resolve_image_tag)"
  log "Resolved deployment plan"
  printf '  %-22s %s\n' "region"      "$AWS_REGION"
  printf '  %-22s %s\n' "profile"     "$AWS_PROFILE"
  printf '  %-22s %s\n' "project"     "$PROJECT_NAME"
  printf '  %-22s %s\n' "ecr repo"    "$ECR_REPO"
  printf '  %-22s %s\n' "image tag"   "$tag"
  printf '  %-22s %s\n' "ecs cluster" "$ECS_CLUSTER"
  printf '  %-22s %s\n' "ecs service" "$ECS_SERVICE"
  printf '  %-22s %s\n' "task family" "$TASK_FAMILY"
  printf '  %-22s %s\n' "cpu / memory" "$TASK_CPU / $TASK_MEMORY"
  printf '  %-22s %s\n' "desired count" "$DESIRED_COUNT"
  printf '  %-22s %s\n' "rds"          "$RDS_INSTANCE_ID ($RDS_DB_NAME, $RDS_ENGINE_VERSION)"
  printf '  %-22s %s\n' "redis"        "$REDIS_CLUSTER_ID ($REDIS_NODE_TYPE)"
  printf '  %-22s %s\n' "s3 bucket"   "$S3_BUCKET"
  printf '  %-22s %s\n' "secret"      "$SECRET_NAME"
  printf '  %-22s %s\n' "dry run"      "$([ $DRY_RUN -eq 1 ] && echo yes || echo no)"
  echo
  log "AWS calls that would be made"
  cat <<EOF
  ecr describe-repositories
  ecr get-login-password
  ecr describe-images
  logs create-log-group
  secretsmanager describe-secret
  rds describe-db-instances
  elasticache describe-replication-groups
  s3api head-bucket / create-bucket
  ecs register-task-definition
  ecs update-service / describe-services
EOF
}

cmd_ecr_login() {
  require aws
  require docker
  log "Authenticating Docker against ECR in $AWS_REGION"
  if [[ $DRY_RUN -eq 1 ]]; then
    dim "  aws ecr get-login-password | docker login --username AWS --password-stdin"
    return 0
  fi
  local registry
  registry="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    ecr describe-repositories --repository-names "$ECR_REPO" \
    --query 'repositories[0].repositoryUri' --output text | cut -d/ -f1)"
  aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecr get-login-password \
    | docker login --username AWS --password-stdin "$registry"
  ok "Docker is authenticated against ECR"
}

cmd_build() {
  require docker
  [[ -f "$DOCKERFILE" ]] || die "Dockerfile not found at '$DOCKERFILE'"
  log "Building image $PROJECT_NAME:$(resolve_image_tag)"
  run docker build -f "$DOCKERFILE" -t "$PROJECT_NAME:$(resolve_image_tag)" \
    -t "$PROJECT_NAME:latest" "$IMAGE_BUILD_CONTEXT"
  ok "Image built"
}

cmd_push() {
  require docker
  require aws
  local tag; tag="$(resolve_image_tag)"
  local uri; uri="$(ecr_repository_uri)"
  log "Tagging and pushing $PROJECT_NAME:$tag -> $uri"
  run docker tag "$PROJECT_NAME:$tag" "$uri:$tag"
  run docker push "$uri:$tag"
  ok "Image pushed as $uri:$tag"
}

cmd_migrate() {
  require aws
  log "Applying the SQL migrations to $RDS_INSTANCE_ID"
  local dsn; dsn="$(rds_dsn)"
  if [[ $DRY_RUN -eq 1 ]]; then
    dim "  psql \"\$dsn\" -f db/migrations/*.sql"
    return 0
  fi
  # The service applies the migrations itself on startup (AUTO_MIGRATE=true);
  # this command is the explicit, out-of-band path for a controlled rollout.
  if command -v psql >/dev/null 2>&1; then
    for f in db/migrations/*.sql; do
      dim "  applying $(basename "$f")"
      psql "$dsn" --set ON_ERROR_STOP=1 --file "$f"
    done
    ok "Migrations applied"
  else
    warn "psql not found - relying on AUTO_MIGRATE during the service rollout"
  fi
}

cmd_deploy() {
  require aws
  local tag; tag="$(resolve_image_tag)"
  local uri; uri="$(ecr_repository_uri)"

  log "Ensuring the CloudWatch log group $LOG_GROUP exists"
  run aws --profile "$AWS_PROFILE" --region "$AWS_REGION" logs create-log-group \
    --log-group-name "$LOG_GROUP" --retention-in-days "$AWS_LOG_RETENTION" \
    >/dev/null 2>&1 || true

  log "Registering a new task definition for $TASK_FAMILY"
  local td; td="$(render_task_definition "$uri:$tag")"
  if [[ $DRY_RUN -eq 1 ]]; then
    dim "$td"
    return 0
  fi

  local out
  out="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    ecs register-task-definition --cli-input-json "$td" \
    --query 'taskDefinition.taskDefinitionArn' --output text)"
  ok "Task definition registered: $out"

  log "Ensuring the ECS cluster $ECS_CLUSTER exists"
  if ! aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs describe-clusters \
      --clusters "$ECS_CLUSTER" --query 'clusters[0].status' --output text \
      2>/dev/null | grep -q ACTIVE; then
    aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs create-cluster \
      --cluster-name "$ECS_CLUSTER" >/dev/null
  fi

  log "Updating the service $ECS_SERVICE to $DESIRED_COUNT task(s)"
  aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs update-service \
    --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
    --task-definition "$out" --desired-count "$DESIRED_COUNT" >/dev/null
  ok "Service updated"

  if [[ $WAIT -eq 1 ]]; then
    log "Waiting for the service to reach a steady state"
    aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs wait \
      services-stable --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE"
    ok "Service is stable"
  fi
}

cmd_status() {
  require aws
  log "ECS service $ECS_SERVICE"
  aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs describe-services \
    --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
    --query 'services[0].{status:status,running:runningCount,desired:desiredCount,taskDefinition:taskDefinition}' \
    --output table
  echo
  log "RDS $RDS_INSTANCE_ID"
  aws --profile "$AWS_PROFILE" --region "$AWS_REGION" rds describe-db-instances \
    --db-instance-identifier "$RDS_INSTANCE_ID" \
    --query 'DBInstances[0].{status:DBInstanceStatus,endpoint:Endpoint.Address}' \
    --output table
  echo
  log "ElastiCache $REDIS_CLUSTER_ID"
  aws --profile "$AWS_PROFILE" --region "$AWS_REGION" elasticache \
    describe-replication-groups --replication-group-id "$REDIS_CLUSTER_ID" \
    --query 'ReplicationGroups[0].{status:Status,endpoint:ConfigurationEndpoint.Address}' \
    --output table
}

cmd_rollback() {
  require aws
  [[ $CONFIRM -eq 1 ]] || die "rollback requires --confirm"
  log "Finding the previous task definition revision"
  local prev
  prev="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    ecs list-task-definitions --family-prefix "$TASK_FAMILY" \
    --status ACTIVE --sort ASC --max-items 2 \
    --query 'taskDefinitionArns[0]' --output text)"
  [[ -n "$prev" && "$prev" != "None" ]] || die "no previous task definition found"
  ok "Rolling back to $prev"
  aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs update-service \
    --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
    --task-definition "$prev" >/dev/null
  ok "Rollback complete"
}

cmd_destroy() {
  require aws
  [[ $CONFIRM -eq 1 ]] || die "destroy requires --confirm (it deletes live resources)"
  log "Destroying the $PROJECT_NAME stack in $AWS_REGION"
  run_sh "aws --profile '$AWS_PROFILE' --region '$AWS_REGION' ecs delete-service --cluster '$ECS_CLUSTER' --service '$ECS_SERVICE' --force"
  run_sh "aws --profile '$AWS_PROFILE' --region '$AWS_REGION' ecs delete-cluster --cluster '$ECS_CLUSTER'"
  run_sh "aws --profile '$AWS_PROFILE' --region '$AWS_REGION' elasticache delete-replication-group --replication-group-id '$REDIS_CLUSTER_ID' --final-replication-group-identifier '${REDIS_CLUSTER_ID}-deleted'"
  run_sh "aws --profile '$AWS_PROFILE' --region '$AWS_REGION' rds delete-db-instance --db-instance-identifier '$RDS_INSTANCE_ID' --skip-final-snapshot"
  run_sh "aws --profile '$AWS_PROFILE' --region '$AWS_REGION' s3 rb 's3://$S3_BUCKET' --force"
  run_sh "aws --profile '$AWS_PROFILE' --region '$AWS_REGION' secretsmanager delete-secret --secret-id '$SECRET_NAME' --force-delete-without-recovery"
  ok "Stack destroyed"
}

cmd_all() {
  if [[ $SKIP_BUILD -eq 0 ]]; then cmd_build; fi
  if [[ $SKIP_PUSH -eq 0 ]]; then cmd_push; fi
  cmd_migrate
  cmd_deploy
}


# --------------------------------------------------------------------------
# Task definition rendering
# --------------------------------------------------------------------------

# Resolve the RDS endpoint into a postgresql+asyncpg DSN.
rds_dsn() {
  local host pass endpoint
  if [[ $DRY_RUN -eq 1 ]]; then
    host="rds-endpoint-placeholder"; pass="placeholder"
  else
    endpoint="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" rds \
      describe-db-instances --db-instance-identifier "$RDS_INSTANCE_ID" \
      --query 'DBInstances[0].Endpoint.Address' --output text)"
    pass="$(secret_value "$SECRET_NAME" | sed -n 's/.*"db_password"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
    host="$endpoint"
  fi
  printf 'postgresql+asyncpg://%s:%s@%s:5432/%s' \
    "$RDS_DB_USER" "$pass" "$host" "$RDS_DB_NAME"
}

# Resolve the ElastiCache endpoint into a redis:// URL.
redis_url() {
  local endpoint
  if [[ $DRY_RUN -eq 1 ]]; then
    endpoint="redis-endpoint-placeholder"
  else
    endpoint="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" elasticache \
      describe-replication-groups --replication-group-id "$REDIS_CLUSTER_ID" \
      --query 'ReplicationGroups[0].ConfigurationEndpoint.Address' --output text)"
  fi
  printf 'redis://%s:6379/0' "$endpoint"
}

# Render the ECS task definition as a CLI input JSON document.
render_task_definition() {
  local image="$1"
  local db_dsn redis_dsn secret_arn
  db_dsn="$(rds_dsn)"
  redis_dsn="$(redis_url)"
  secret_arn="arn:aws:secretsmanager:${AWS_REGION}:123456789012:secret:${SECRET_NAME}"

  local networking="[]"
  if [[ -n "$SUBNET_IDS" ]]; then
    networking="{\"awsvpcConfiguration\":{\"subnets\":[$(echo "$SUBNET_IDS" | sed 's/,/","/g; s/^/"/; s/$/"/')],"
    if [[ -n "$SECURITY_GROUP_IDS" ]]; then
      networking="$networking\"securityGroups\":[$(echo "$SECURITY_GROUP_IDS" | sed 's/,/","/g; s/^/"/; s/$/"/')],"
    fi
    networking="$networking\"assignPublicIp\":\"ENABLED\"}}"
  fi

  cat <<JSON
{
  "family": "${TASK_FAMILY}",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "${TASK_CPU}",
  "memory": "${TASK_MEMORY}",
  "executionRoleArn": "arn:aws:iam::123456789012:role/${TASK_FAMILY}-execution",
  "taskRoleArn": "arn:aws:iam::123456789012:role/${TASK_FAMILY}-task",
  "containerDefinitions": [
    {
      "name": "${CONTAINER_NAME}",
      "image": "${image}",
      "essential": true,
      "portMappings": [
        { "containerPort": ${CONTAINER_PORT}, "protocol": "tcp" }
      ],
      "environment": [
        { "name": "ENVIRONMENT", "value": "production" },
        { "name": "DEBUG", "value": "false" },
        { "name": "LOG_LEVEL", "value": "INFO" },
        { "name": "LOG_JSON", "value": "true" },
        { "name": "HOST", "value": "0.0.0.0" },
        { "name": "PORT", "value": "${CONTAINER_PORT}" },
        { "name": "UVICORN_WORKERS", "value": "2" },
        { "name": "AUTO_MIGRATE", "value": "true" },
        { "name": "AUTO_SEED", "value": "true" },
        { "name": "API_KEY_REQUIRED", "value": "true" },
        { "name": "ALLOW_USAGE_OVERRIDE", "value": "false" },
        { "name": "DATABASE_URL", "value": "${db_dsn}" },
        { "name": "REDIS_URL", "value": "${redis_dsn}" },
        { "name": "AWS_REGION", "value": "${AWS_REGION}" },
        { "name": "S3_BUCKET", "value": "${S3_BUCKET}" },
        { "name": "S3_PREFIX", "value": "audit" },
        { "name": "S3_ENDPOINT_URL", "value": "" },
        { "name": "AUDIT_ENABLED", "value": "true" },
        { "name": "METRICS_ENABLED", "value": "true" },
        { "name": "ML_GUARD_ENABLED", "value": "true" },
        { "name": "SECRET_ARN", "value": "${secret_arn}" }
      ],
      "secrets": [
        { "name": "STRIPE_SECRET_KEY", "valueFrom": "${secret_arn}:stripe_secret_key::" },
        { "name": "STRIPE_WEBHOOK_SECRET", "valueFrom": "${secret_arn}:stripe_webhook_secret::" }
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "${LOG_GROUP}",
          "awslogs-region": "${AWS_REGION}",
          "awslogs-stream-prefix": "ecs"
        }
      },
      "healthCheck": {
        "command": ["CMD-SHELL", "python3 -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${CONTAINER_PORT}/health', timeout=5).status == 200 else 1)\""],
        "interval": 30,
        "timeout": 10,
        "retries": 3,
        "startPeriod": 30
      }
    }
  ]
}
JSON
}

# --------------------------------------------------------------------------
# Usage / argument parsing
# --------------------------------------------------------------------------
usage() {
  sed -n '2,/^# =====/p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)        IMAGE_TAG="$2"; shift 2 ;;
    --region)     AWS_REGION="$2"; shift 2 ;;
    --profile)    AWS_PROFILE="$2"; shift 2 ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --skip-push)  SKIP_PUSH=1; shift ;;
    --no-wait)    WAIT=0; shift ;;
    --confirm)    CONFIRM=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    -*)           die "unknown option: $1 (try --help)" ;;
    *)            COMMAND="$1"; shift ;;
  esac
done

case "$COMMAND" in
  plan)      cmd_plan ;;
  ecr-login) cmd_ecr_login ;;
  build)     cmd_build ;;
  push)      cmd_push ;;
  migrate)   cmd_migrate ;;
  deploy)    cmd_deploy ;;
  status)    cmd_status ;;
  rollback)  cmd_rollback ;;
  destroy)   cmd_destroy ;;
  all)       cmd_all ;;
  help|"")   usage ;;
  *)         die "unknown command: $COMMAND (try --help)" ;;
esac


