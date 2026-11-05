# Private AWS deployment

This deployment is for **one active backend** in `ap-south-1`. It initially
uses synthetic Dhan credentials, an empty separate data volume, dry-run, PIN
required, and disarmed new entries. It does not import the local `.env`, audit
database, journals, positions, or broker credentials. Verify the actual AWS
stack and its status; the presence of these files does not mean it is deployed
or production-ready.

## What is created

- One 1 GiB instance, a 12 GiB encrypted root disk and a separate 4 GiB
  encrypted data disk. New stacks default to ARM `t4g.micro`; the current
  Mumbai mock stack uses the approved x86 `t3.micro` after ARM capacity was
  unavailable. The image architecture must match the instance type. The data
  volume is retained on ordinary stack
  deletion/replacement; it is deleted if the initial creation rolls back.
  A single server is not highly available. Validate memory, CPU credits and
  response times under realistic trading load; future strategies may need a
  larger instance.
- One static public IPv4 for outbound requests. Its security group has **no
  inbound rules**. Nginx and FastAPI listen only on loopback. Access the web UI
  through an IAM-authorized SSM port-forwarding session, never a public app port.
  The existing application login checks the broker token, not visitor identity.
- CloudWatch application/system logs (30 days), 7-day local debug logs, memory,
  disk, CPU-credit and EC2 alarms. Journals and trading audit retention have
  separate behavior from these debug logs. Detailed logs can contain financial
  information; limit IAM access.
- AWS Backup of the data volume at 16:20 India time on weekdays, with 14-day
  retention, and alerts for failed backup jobs. Verify an actual recovery point
  and restore before relying on backups. The root disk and artifact bucket are
  not part of this backup.
- A separate AWS Lambda/CloudWatch health check and metadata-only Telegram
  alerts. During the trading window it checks server state, SSM, the backend,
  Nginx and the log agent, not the broker feed, order delivery, fill status or
  open-position safety. A stopped host cannot send its own alerts. Telegram
  delivery can fail; inspect the alert dead-letter queue and CloudWatch alarm.
- An optional, separately enabled **15:55 IST Monday-Friday** AWS usage report
  sends the user's AWS account-wide estimated cost to the configured private
  Telegram destination. It reports the current **UTC** day and UTC month,
  not an exact Indian-calendar-day invoice; a UTC day starts at 05:30 IST.
  Cost Explorer refreshes at least daily, individual data can take longer than
  24 hours, and a newly enabled account can take about 24 hours to initialize.
  Missing or zero data does not prove there are no charges. Credits, tax and
  final payment may differ. The report's EC2 time is only an approximation
  for the **current instance**, based on observed 5-minute CPU metric periods;
  it excludes an earlier instance replaced on the same day. Partial periods,
  missing data or delays can overstate or understate actual runtime. Each Cost Explorer API
  request costs approximately **USD 0.01**; retries can add charges.
- A monthly **account-wide** USD 20 budget with actual/forecast alerts as an
  early warning. It is not a spending limit, and INR conversion, tax, credits
  and   billing currency depend on the account. EBS, backups and some monitoring
  charges continue while EC2 is stopped. Every public IPv4 address costs about
  USD 0.005/hour; AWS sells no daily or monthly IP plan. An Elastic IP is billed
  every hour it is allocated, even while EC2 is stopped, whereas the automatic
  public IP is billed only while the instance runs.

## Static IP for broker whitelisting

`StaticIpEnabled=true` (the template default) allocates an Elastic IP. Mock
mode does not need a broker-whitelisted IP, so it can be released while the
instance is stopped:

```powershell
python .\deployment\aws\deploy.py release-static-ip --approved-static-ip-release
```

The reviewed change set may only remove the Elastic IP and its association.
Release is irreversible: the same address cannot be guaranteed later. The
instance keeps outbound internet through its automatic public IP, which changes
on each stop/start and must never be registered with a broker.

To keep one or two broker-whitelisted Elastic IPs, preview and then apply with
the instance stopped:

```powershell
python .\deployment\aws\deploy.py set-static-ips --count 2 --preview
python .\deployment\aws\deploy.py set-static-ips --count 2 --approved-static-ip-change
```

The preview creates and deletes a change set without buying anything. The
reviewer allows only the static-IP resource delta. `--count 2` adds a second
network interface (ens6) with its own Elastic IP. For separate account routing,
register `StaticOutboundIp` as the Master account's primary IP and
`SecondaryStaticOutboundIp` as the Child account's primary IP in Dhan. These
output names describe the server interfaces, not Dhan's primary/secondary
registration fields. Configure each account's private source IP as described
below; without those settings both accounts use the OS default route. Each
address costs about USD 0.005/hour, 24x7, until it is released with a lower
`--count`. Instance replacement commands refuse changes while the second
interface exists.

Verify from the instance through SSM:

```bash
curl -s https://checkip.amazonaws.com                         # primary
curl -s --interface <ens6-private-ip> https://checkip.amazonaws.com  # secondary
```

## Going live (real Dhan credentials)

```powershell
python .\deployment\aws\deploy.py push-live-credentials --approved-live-trading [--keep-dry-run]
```

This reads the Master/Child Dhan values and `APP_ORDER_PIN` from the local
`backend\.env`, stores them in the encrypted `/<stack>/dhan` SecureString,
grants only the instance role read access (a reviewed change that may modify
only `InstanceRole`), rewrites the managed keys in the instance `.env` through
SSM without placing secrets in the command, and restarts the service. Rerun it
after refreshing a Dhan token locally. Without `--keep-dry-run` real orders are
enabled (`APP_DRY_RUN=false`). Existing `APP_FAST_ORDERS`,
`TRADING_DEFAULT_ACCOUNTS` and `ALGO_AUTO_ARM` values in the instance `.env`
are preserved (fast orders default to `false` only when absent).
Stop the local backend first: never run two engines against one Dhan account.

The app refuses to start when saved algo state belongs to another Dhan client
ID. Mock state from the first deployment was archived on the instance under
`/var/lib/trading/mock-archive-20261102`. Mock-only commands (`apply-mock-release`)
refuse to run once live credentials are installed.

Register each account's own static IP under DhanHQ Trading APIs > Static IP.
Verify that Dhan reports the expected `detectedIP` and `ordersAllowed=true`
for each account before trading. Missing registration can produce
`IP_NOT_VERIFIED`; mismatched registration prevents order authorization.

### Per-account outbound IP

`DHAN_MASTER_SOURCE_IP` and `DHAN_CHILD_SOURCE_IP` in the instance `.env` bind
each account's Dhan REST, order-update WebSocket and IP-detection traffic to a
local private IP. Current mapping: Master `172.31.42.150` (ens5, EIP
`13.201.158.130`), Child `172.31.41.228` (ens6, EIP `52.66.160.118`). Register
each account's EIP as its primary static IP in Dhan. Leave both empty to use the
OS default route. `/api/account/ip` with `X-Trading-Account: child` shows the IP
Dhan sees for that account.

## Schedule and trading safety

The start/stop schedules are created **disabled**. Once verified and enabled,
EventBridge Scheduler requests start at **09:00** and stop at **16:00
Asia/Kolkata, Monday-Friday**. Invocation has up to 60-second precision; boot,
application readiness and shutdown take additional time. No exchange-holiday
exclusions are configured. Add the owner's holiday list before relying on
holiday handling. An EC2 start can fail if AWS has insufficient capacity in
the instance's availability zone; the schedule does not reserve capacity or
guarantee that the dashboard is ready by 09:00. The start scheduler retries
failed requests for at most 15 minutes; the stop schedule retains its shorter
retry window. Investigate scheduler and availability alerts and verify
readiness before trading.

The stop is intentional even if positions, orders or their status remain open
or unknown. The AWS stop schedule does **not** square off positions. Separately,
the existing ORB strategy may request a time-based exit at its configured
exit time (default 15:29 IST) before the server stops; verify strategy settings
and broker fills rather than assuming an exit succeeded. At and after shutdown,
software-managed exits, feed monitoring and application alerts stop; a
next-morning reconciliation cannot protect overnight exposure. Always inspect
the broker's actual positions and orders before taking further action.

On application startup, new algo entries are disarmed, but a persisted live
position may still be managed according to its original execution mode.

### Unattended daily startup (opt-in)

`TRADING_DEFAULT_ACCOUNTS=master,child` selects the order/algo recipients at
every startup (default `master`). With `ALGO_AUTO_ARM=true` the app then runs
the same guarded shared ARM as the Algo tab: each account must pass the live
static-IP preflight, an account that fails is skipped, and ARM is refused while
a position is tracked. Telegram receives `auto_armed` or `auto_arm_failed`.
Current owner-approved live setting: `APP_DRY_RUN=false`, `APP_FAST_ORDERS=true`
(no PIN), both accounts, auto-arm on. Auto-arm cannot succeed with an expired
Dhan token, so tokens must be refreshed before each trading day. Set
`ALGO_AUTO_ARM=false` to return to manual ARM.

### Position sizing and rupee exits

`ALGO_MAX_AFFORDABLE=true` (or the Settings tab "Max lots" checkbox) sizes each
account at `floor(balance x 90% / (premium x lot size))` lots. `Max lots`,
`risk_pct` and `max_capital_pct` are ignored; the only bound is the exchange
per-order freeze quantity (`ALGO_FREEZE_QTY`, default NIFTY 1800, BANKNIFTY 900,
SENSEX 1000), because a larger single order is rejected outright. Example: balance
15000, lot 65, premium 100 -> 2 lots (130 qty). The Algo tab "Exit conditions"
box sets a profit and a stop-loss in rupees (0 = off) for the ticked accounts
(`accounts` in `POST /api/algo/exit-rules`; each account keeps its own values,
also across ARM); it can be changed while armed or holding. The Master/Child
ticks can also change while armed: unticking stops new entries for that
account (its open position is still managed), ticking joins after the IP check. Each
account exits with a MARKET SELL when its own gross premium P&L reaches
`+profit` (`PROFIT_RS`) or `-loss` (`LOSS_RS`), checked only on a fresh (<5 s)
option price. The spot target and the 15:29 time exit still apply.

### Bootstrap application package

```powershell
python .\deployment\aws\deploy.py publish-app-artifact --approved-app-publish
```

Builds the frontend, uploads the current code to S3 and updates only
`AppArtifactKey` through a reviewed change set. It refuses unless the instance
is stopped (the pointer rewrites user data). The package is used only when an
instance is rebuilt; the running server is updated separately, and a rebuilt
instance still needs `push-live-credentials`.

Merely setting dry-run does not make an imported live state harmless. Never copy or
restore old trading state blindly, and never run local and cloud engines against
the same account simultaneously. Real Dhan credentials, static-IP registration,
state migration, broker reconciliation and LIVE/ARM each require separate
verification and explicit operator approval.

The mock dashboard visibly labels synthetic market data and mock broker
accounts. These labels do not imply that another configured account is mock:
real account credentials can send real orders when LIVE mode is selected.
Do not use synthetic prices for trading decisions.

## Create the mock stack

Install the requirements in a dedicated local Python environment and use
temporary AWS credentials. An AWS CLI `aws login` profile needs the CRT extra
provided by `requirements.txt`; never use or share long-lived root access keys.
The deployment uses account permissions to create billable resources. The
owner must configure payment in AWS Billing.

From the repository root in PowerShell:

```powershell
python -m venv deployment\aws\.venv
.\deployment\aws\.venv\Scripts\python.exe -m pip install -r deployment\aws\requirements.txt
.\deployment\aws\.venv\Scripts\python.exe deployment\aws\deploy.py --profile trading-deploy --stack trading-dashboard create
```

The script builds the frontend, packages an allowlist without secrets or
runtime state, creates/reuses an encrypted private artifact bucket, stores only
the Telegram token/chat ID in a SecureString parameter, and creates the stack.
The generated server `.env` uses a new random session secret and mock-only PIN.
Do not print or paste the SecureString or `.env` into chat. The script refuses
to overwrite an existing different Telegram parameter or stack.

CloudFormation completion does **not** prove the instance bootstrap succeeded.
Check the EC2 instance, SSM registration, service health, mock-mode settings,
security group, cloud logs, external Telegram delivery, backups and a restore.
The schedules must remain disabled until these checks pass. After an operator
has actually completed all checks:

```powershell
.\deployment\aws\.venv\Scripts\python.exe deployment\aws\deploy.py --profile trading-deploy --stack trading-dashboard enable-automation --verified-private-mock-alerts-and-restore
```

This confirmation flag is an operator attestation, not an automated proof of a
restore. Do not use it to bypass the checks. After provisioning outside the
trading window, stop the test instance so it does not run overnight.

To pause an isolated mock deployment indefinitely, stop the instance first and
confirm EC2 reports it as stopped. Then run:

```powershell
.\deployment\aws\.venv\Scripts\python.exe deployment\aws\deploy.py --profile trading-deploy --stack trading-dashboard pause-automation
```

The command refuses to disable the recurring 16:00 stop while the instance is
running. It disables both recurring start and stop schedules and the scheduled
availability monitor, so a manual start will not automatically stop at 16:00
while paused. The independently scheduled cost report and backup remain
enabled, and stopped resources can still incur charges. Re-enable automation
only after checking the current instance, alert delivery, private access and
backup status.

The cost report is controlled independently of the EC2 start/stop schedules.
After a test message was actually delivered to the intended private channel
and inspected, enable only the report with:

```powershell
.\deployment\aws\.venv\Scripts\python.exe deployment\aws\deploy.py --profile trading-deploy --stack trading-dashboard enable-cost-report --verified-cost-report
```

The `--verified-cost-report` flag is an operator attestation, not an automated
delivery check. The report can still say that billing data is not yet available.
If the Lambda fails, CloudWatch sends a separate generic failure alert; failed
events go to the scheduler dead-letter queue. No raw broker information or
credentials are included in the cost report.

For an existing mock stack, `update-mock --reporting-only` updates the Lambda
artifacts and reporting infrastructure while preserving the previous app and
bootstrap artifact references. The change-set guard refuses any instance
change; this command does not repair or deploy the application. A full
`update-mock` also changes instance user data and can cause an EC2 restart.
If an update or rollback fails because capacity is unavailable, inspect the
CloudFormation events and retry the normal rollback; do not skip resources
without first resolving the resulting configuration drift.

For a Lambda-code-only update to the isolated mock stack, including while
weekday automation is enabled, use:

```powershell
.\deployment\aws\.venv\Scripts\python.exe deployment\aws\deploy.py --profile trading-deploy --stack trading-dashboard update-mock-lambda --approved-mock-lambda-update
```

This reuses the deployed template and approves only reviewed Lambda code
changes and their expected reference-dependent updates. It keeps Lambda
settings, the instance and schedule configuration unchanged. Afterward, invoke
and inspect the affected function; a successful stack update alone does not
prove that the function works.

## Access and logs

The stack creates a separate IAM user named `<stack>-operator` without a console
password, access keys or an MFA device. Its role trust allows **only that user
with MFA**. The user can assume the role, use browser-based AWS CLI login and
manage their own password; the role allows private port forwarding, reading
the relevant logs and starting/stopping this instance. It cannot read secrets
or run arbitrary SSM shell commands. A temporary root bootstrap profile is not
a routine operator identity.

The account owner must complete enrollment directly in AWS, without sharing
the password, MFA QR code, secret key or one-time codes:

1. Sign in as the account administrator and open **IAM > Users**. Select the
   `OperatorUserName` from the stack outputs.
2. Under **Security credentials > Console sign-in**, choose **Enable console
   access** and set a private password. Require a password change at next
   sign-in if using an initial password.
3. Under **Multi-factor authentication (MFA)** choose **Assign MFA device**.
   To use the CLI `mfa_serial` flow below, enroll an authenticator app and
   complete the two-code verification directly in AWS.

Only after enrollment, install a current AWS CLI and the official
[Session Manager plugin for Windows](https://docs.aws.amazon.com/systems-manager/latest/userguide/install-plugin-windows.html).
In a browser session signed in as the IAM user, run `aws login --profile
trading-login`. The resulting login must identify the operator IAM user, not
the root user. Obtain the user's MFA serial with:

```powershell
aws iam list-mfa-devices --user-name trading-dashboard-operator --profile trading-login --query "MFADevices[0].SerialNumber" --output text
```

Configure `trading-operator` with `role_arn` set to the stack's
`OperatorRoleArn`, `source_profile` set to `trading-login`, and `mfa_serial`
set to that device serial. For example:

```powershell
aws configure set role_arn ROLE_ARN_FROM_STACK_OUTPUT --profile trading-operator
aws configure set source_profile trading-login --profile trading-operator
aws configure set mfa_serial MFA_DEVICE_SERIAL --profile trading-operator
aws sts get-caller-identity --profile trading-operator
```

Enter the one-time MFA code only in the local AWS CLI prompt. Verify that
`get-caller-identity` reports the operator role, then verify private access
and log reading. These instructions describe the intended flow; do not treat
the operator account as operational until this actual login and role test has
succeeded. For console log access, sign in as the IAM user with MFA and switch
to the `OperatorRoleArn` role before opening CloudWatch.

With an AWS CLI profile named `trading-operator`, run:

```powershell
.\deployment\aws\Open-Dashboard.ps1 -Profile trading-operator -InstanceId i-REPLACE
```

Keep that window open and visit `http://127.0.0.1:8080`. The connection from the
computer to AWS is protected by the SSM session; the local HTTP port is not a
separate authentication boundary. Secure the local computer and do not forward
the port to other network interfaces.

```powershell
aws logs tail /trading-dashboard/trading-dashboard/app --follow --profile trading-operator --region ap-south-1
aws logs tail /trading-dashboard/trading-dashboard/system --follow --profile trading-operator --region ap-south-1
aws cloudwatch describe-alarms --profile trading-operator --region ap-south-1
```

The first log group has `app`, `orders`, `access` and `errors` streams; the
second includes bootstrap and Nginx errors. For delivery failures inspect the
alert Lambda logs and its dead-letter queue with an appropriately privileged
administrator. A manual start outside the schedule is not immediately stopped
by the scheduler and can incur overnight charges; stop it explicitly.

## Updates and recovery

The initial user-data bootstrap runs once. Replacing an S3 artifact or updating
the instance user data alone does **not** deploy an application update.

For the isolated mock deployment only, the guarded `prepare-mock-release`
and `apply-mock-release` commands update the stack's immutable app artifact and
install its backend/frontend. They require a completed encrypted backup of the
actual data volume, disabled normal automation, a verified same-day 16:00 safety
stop, no inbound security-group rules and mock-only credentials. Start before
15:30 IST on a weekday. Stop the disarmed mock instance before preparing the
release; CloudFormation may restart it. After it is running and SSM is online:

```powershell
.\deployment\aws\.venv\Scripts\python.exe deployment\aws\deploy.py --profile trading-deploy --stack trading-dashboard prepare-mock-release --data-recovery-point RECOVERY_POINT_ARN --approved-mock-release
.\deployment\aws\.venv\Scripts\python.exe deployment\aws\deploy.py --profile trading-deploy --stack trading-dashboard apply-mock-release --data-recovery-point RECOVERY_POINT_ARN --approved-mock-release
```

The release verifies the artifact hash, refuses changed Python dependencies or
service/monitoring configuration, validates Nginx, checks the mounted data
volume and mock runtime, and retains the previous app and frontend for rollback.
It is **not** a live-trading deployment procedure. Recheck private browser
access, mock labels, logs, alerts and data after every release. The frontend
entry point is served without caching so an old browser build cannot hide a
new safety warning; a browser that cached an older version may need a hard
refresh once.

On recovery, restore the encrypted data volume from a verified recovery point,
attach it in the correct availability zone, and reconcile with the broker
before starting a live-capable engine. Do not start a second active engine.
The retained data volume and backup vault can continue to incur charges after
the stack is deleted; the artifact bucket and Telegram parameter are also
managed separately. Before deleting the stack, securely remove any manually
enrolled operator password and MFA device as required by IAM; otherwise user
deletion can fail. Delete data or credentials only after the owner confirms
that they are no longer needed and has another administrative sign-in.
