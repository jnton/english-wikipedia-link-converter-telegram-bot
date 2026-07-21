import os

from aws_setup.common import client
from aws_setup.lambda_resources import disable_legacy_api_gateway_invocation


def main() -> None:
    region = os.environ["AWS_REGION"]
    function_name = os.getenv("LAMBDA_FUNCTION_NAME", "ToEnWikipediaBot")
    lambda_client = client("lambda", region)
    disable_legacy_api_gateway_invocation(lambda_client, function_name)
    print("Legacy API Gateway invocation permission removed after successful smoke test.")


if __name__ == "__main__":
    main()
