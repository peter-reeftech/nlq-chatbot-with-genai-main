import os
from constructs import Construct
from aws_cdk import (
    Stack,
    aws_ecs as ecs,
    aws_ecs_patterns as ecs_patterns,
    aws_iam as iam,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    RemovalPolicy,
    aws_glue as glue,
    aws_athena as athena,
    CfnOutput
)
from aws_cdk.aws_ecr_assets import DockerImageAsset
import json
from cdklabs.generative_ai_cdk_constructs import (
    bedrock
)

class EcsFargateStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        
        # Create an S3 bucket
        data_bucket = s3.Bucket(self, "DataBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True
        )

        # Get the path to the 'example-data' folder in the parent directory
        current_dir = os.path.dirname(os.path.realpath(__file__))
        example_data_dir = os.path.abspath(os.path.join(current_dir, '..', 'example-data'))

        # Deploy the 'example-data' folder to the S3 bucket
        s3deploy.BucketDeployment(self, "DeployFolder",
            sources=[s3deploy.Source.asset(example_data_dir)],
            destination_bucket=data_bucket,
            retain_on_delete=False  # This will delete the files from S3 when the stack is destroyed
        )

        # Create an IAM role for the Glue crawler
        crawler_role = iam.Role(self, "GlueCrawlerRole",
            assumed_by=iam.ServicePrincipal("glue.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSGlueServiceRole")
            ]
        )

        # Grant the crawler role read access to the S3 bucket
        data_bucket.grant_read(crawler_role)

        # Define constant values
        ATHENA_DATABASE_NAME = "example_glue_database"
        ATHENA_WORKGROUP_NAME = "primary_workgroup"

        # Create a Glue database
        glue_database = glue.CfnDatabase(self, "GlueDatabase",
            catalog_id=self.account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=ATHENA_DATABASE_NAME
            )
        )

         # Create a Glue crawler
        glue_crawler = glue.CfnCrawler(self, "ExampleGlueCrawler",
            name="example-data-crawler",
            role=crawler_role.role_arn,
            database_name=ATHENA_DATABASE_NAME,
            targets=glue.CfnCrawler.TargetsProperty(
                s3_targets=[glue.CfnCrawler.S3TargetProperty(
                    path=f"s3://{data_bucket.bucket_name}/",
                    exclusions=[".*"],  # This line excludes hidden files and directories
                )]
            ),
            schema_change_policy=glue.CfnCrawler.SchemaChangePolicyProperty(
                update_behavior="UPDATE_IN_DATABASE",
                delete_behavior="DEPRECATE_IN_DATABASE"
            ),
            schedule=glue.CfnCrawler.ScheduleProperty(
                schedule_expression="cron(0 * * * ? *)"  # Run hourly
            )
        )

        # Ensure the crawler is created after the database
        glue_crawler.add_dependency(glue_database)

         # Create an S3 bucket for Athena query results
        athena_results_bucket = s3.Bucket(self, "AthenaResultsBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True
        )

        # Create Athena workgroup
        athena_workgroup = athena.CfnWorkGroup(self, "AthenaWorkgroup",
            name=ATHENA_WORKGROUP_NAME,
            recursive_delete_option=True,
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{athena_results_bucket.bucket_name}/athena-results/"
                )
            )
        )

        athena_staging_dir = f"s3://{athena_results_bucket.bucket_name}/athena-results/"
        athena_connection_string = f"awsathena+rest://@athena.{self.region}.amazonaws.com:443/{ATHENA_DATABASE_NAME}?s3_staging_dir={athena_staging_dir}&work_group={ATHENA_WORKGROUP_NAME}"

        ### PROMPT ###
        # Define prompt variant
        variant1 = bedrock.PromptVariant.text(
            variant_name="variant1",
            model=bedrock.BedrockFoundationModel.ANTHROPIC_CLAUDE_HAIKU_V1_0,
            template_configuration={
                #"input_variables": [{"name": "dialect"}],
                "text": 
f"""You are a data analyst that analyses data in the database, and provides stats and analysis to users. 
You have access to a Trino database, which contains a tables of data. 

Follow the below steps when querying the database:

1. If you need to query the database, list the tables first and their columns in the database to see what you can query then create a syntactically correct {{dialect}} query to run. 

2. If you get an error while executing a query, rewrite the query and try again. 

3. Look at the results of the query and return the answer to the question directly in plain english with no tags. 


Here are some extra tips you can use if you get stuck: 
- Do not use the DATE_SUB function in your query, use the date_add function instead using the following format: 
SELECT assetid 
FROM example-data-crawler 
WHERE CAST(sensortimestamp AS timestamp) > date_add('hour', -24, CAST(CURRENT_TIMESTAMP AS timestamp)); 
Use the FLOAT type in DDL statements like CREATE TABLE and the REAL type in SQL functions like SELECT CAST. 

- Always assume the year is 2024.
""",
            },
            inference_configuration={
                "temperature": 0.5,
                "top_p": 0.999,
                "max_tokens": 2000,
                "top_k": 250,
            }
        )

        # Create prompt
        prompt = bedrock.Prompt(
            self,
            "GenAIPrompt",
            prompt_name="GenAIPrompt",
            description="GenAI Prompt",
            default_variant=variant1,
            variants=[variant1],
        )
        
        
        ### CHAINLIT DOCKER & ECS ###
        # Build the Docker image
        image = DockerImageAsset(self, "DockerImage",
            directory=os.path.join(os.path.dirname(__file__), "..", ".."),
            file="Dockerfile",
        )

        # Create an IAM role for the Fargate task
        task_role = iam.Role(self, "TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com")
        )

        # Add permissions to the role
        task_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AdministratorAccess"))

        # Create the Fargate service with an Application Load Balancer
        fargate_service = ecs_patterns.ApplicationLoadBalancedFargateService(self, "GenAIService",
            memory_limit_mib=2048,
            cpu=1024,
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ecs.ContainerImage.from_docker_image_asset(image),
                container_port=8080,  # Adjust this to match your container's exposed port
                task_role=task_role,  # Assign the IAM role to the task
                environment={
                    "ATHENA_CONNECTION_STRING": athena_connection_string,
                    "BEDROCK_PROMPT_ID": prompt.prompt_id
                },
            ),
            desired_count=1,
            public_load_balancer=True
        )

        # Add outputs for easy access to environment variables
        CfnOutput(self, "AthenaConnectionString",
                  value=athena_connection_string,
                  export_name="AthenaConnectionString")
        CfnOutput(self, "BedrockPromptId",
                  value=prompt.prompt_id,
                  export_name="BedrockPromptId")