# One-time AWS bootstrap for GitHub Actions

The production workflow currently authenticates with the long-lived IAM user `GitHub_Deployer`. That user can update the existing Lambda, but AWS denied `sqs:CreateQueue`, so the durable free-tier architecture cannot be provisioned.

Do **not** grant broad deployment permissions permanently to this IAM user. Instead, give it the small temporary policy in [`aws-github-deployer-one-time-bootstrap-policy.json`](./aws-github-deployer-one-time-bootstrap-policy.json). The policy allows the user only to create or repair:

- the GitHub Actions OIDC provider;
- the repository-specific `GitHubActions-ToEnWikipediaBot` role;
- that role's inline least-privilege deployment policy;
- the read operations needed to determine the existing Lambda execution role.

The deployment role itself is restricted to this repository's `main` branch by its OIDC trust policy. After bootstrap, GitHub Actions exchanges its GitHub OIDC token for short-lived AWS credentials rather than using the IAM user's stored access keys.

## Attach the temporary policy

1. Sign in to the AWS Console with an administrator or another principal allowed to manage IAM policies.
2. Open **IAM**.
3. Open **Users** → **GitHub_Deployer**.
4. Open **Permissions** → **Add permissions** → **Create inline policy**.
5. Select the **JSON** editor.
6. Paste the complete contents of `docs/aws-github-deployer-one-time-bootstrap-policy.json`.
7. Choose **Next** and name the policy `ToEnWikipediaBotOneTimeOidcBootstrap`.
8. Create the policy.

## Trigger and verify deployment

After attaching the policy, rerun the latest failed GitHub Actions workflow or push an empty commit to `main`.

A successful run must show:

- `Attempt non-blocking GitHub OIDC bootstrap` completed without an error;
- `Switch to short-lived OIDC credentials when bootstrap succeeds` ran;
- queue, state, worker, monitor, schedule, alarms, and smoke-test steps succeeded;
- `Remove legacy API Gateway route after smoke test` succeeded.

The workflow automatically restores the authenticated known-good bot if any later step fails.

## Remove temporary access

Only after the full deployment is green:

1. Return to **IAM** → **Users** → **GitHub_Deployer** → **Permissions**.
2. Delete the inline policy `ToEnWikipediaBotOneTimeOidcBootstrap`.
3. Confirm a later workflow still authenticates through `GitHubActions-ToEnWikipediaBot`.
4. Then remove the `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` repository secrets, or deactivate/delete the corresponding IAM access key.

Do not remove the access key before one complete OIDC deployment succeeds, because it is currently the recovery credential.

## Evidence for the current blocker

The sanitized deployment artifact for GitHub Actions run `29835435984` reported:

- stage: `Create or update SQS queues`;
- operation: `CreateQueue`;
- AWS error: `AccessDenied`;
- principal: `arn:aws:iam::590183760311:user/GitHub_Deployer`;
- denied action: `sqs:CreateQueue`;
- resource: `arn:aws:sqs:eu-north-1:590183760311:ToEnWikipediaBot-dlq`.

Production was automatically restored afterward using the known-good authenticated Lambda package and Telegram secret-bound Function URL webhook.
