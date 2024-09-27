import boto3
import chainlit as cl
from chainlit.input_widget import Switch
import os
import uuid
from langchain.schema.runnable.config import RunnableConfig
from langchain_aws import ChatBedrock
from langchain_core.messages import SystemMessage
from sqlalchemy import create_engine
from langchain_community.utilities import SQLDatabase
from langgraph.prebuilt import create_react_agent
from langchain_community.agent_toolkits.sql.toolkit import SQLDatabaseToolkit
from langchain_community.tools import DuckDuckGoSearchRun
from langgraph.checkpoint.memory import MemorySaver
from utils.message_trimming import modify_state_messages
from utils.token_counter import TokenCounter

memory = MemorySaver()

# Environment Variables
prompt_id = os.environ['BEDROCK_PROMPT_ID']
connection_string = os.environ['ATHENA_CONNECTION_STRING']


bedrock_runtime = boto3.client(
    service_name="bedrock-runtime",
    # use export AWS_DEFAULT_REGION=ap-southeast-2 # region_name="us-east-1"
)

# Model configuration
model_id = "anthropic.claude-3-haiku-20240307-v1:0"
model_kwargs = {
    "max_tokens": 2048, "temperature": 0.1,
    "top_k": 250, "top_p": 1, "stop_sequences": ["\n\nHuman"],
}
model = ChatBedrock(
    client=bedrock_runtime,
    model_id=model_id,
    model_kwargs=model_kwargs,
)

# Prompt client configuration
bedrock_agent_client = boto3.client(
    service_name="bedrock-agent", 
    # use export AWS_DEFAULT_REGION=ap-southeast-2 # region_name="us-east-1"
)


@cl.on_chat_start
async def start():
    thread_id = str(uuid.uuid4())
    cl.user_session.set("thread_id", thread_id)
    cl.user_session.set("token_counter", TokenCounter())

    response = bedrock_agent_client.get_prompt(promptIdentifier=prompt_id)
    default_variant = response['defaultVariant']
    variants = {v['name']: v for v in response['variants']}
    system_prompt = variants[default_variant]['templateConfiguration']['text']['text']

    system_message = SystemMessage(
        content=system_prompt.format(dialect="trino"))

    def state_modifier(state):
        return modify_state_messages(state, model, system_message)

    # DB Connection and tools
    engine_athena = create_engine(connection_string, echo=False)
    db = SQLDatabase(engine_athena)

    toolkit = SQLDatabaseToolkit(db=db, llm=model)
    sql_tools = toolkit.get_tools()
    #search_tool = DuckDuckGoSearchRun()
    tools = sql_tools #+ [search_tool]


    agent_executor = create_react_agent(
        model,
        tools,
        state_modifier=state_modifier,
        checkpointer=memory
    )

    cl.user_session.set("runnable", agent_executor)
    cl.user_session.set("system_message", system_message)

    await cl.ChatSettings([
        Switch(id="ShowTokenCount", label="Show Token Count", initial=False),
        Switch(id="EnableTrimming", label="Enable Message Trimming", initial=True),
    ]).send()


@cl.on_settings_update
async def setup_agent(settings):
    cl.user_session.set("show_token_count", settings["ShowTokenCount"])
    cl.user_session.set("enable_trimming", settings["EnableTrimming"])


@cl.on_message
async def on_message(message: cl.Message):
    agent_executor = cl.user_session.get("runnable")
    thread_id = cl.user_session.get("thread_id")
    token_counter = cl.user_session.get("token_counter")

    async for chunk in agent_executor.astream(
        {"messages": [("human", message.content)]},
        config=RunnableConfig(callbacks=[cl.LangchainCallbackHandler()], configurable={
            "thread_id": thread_id,
            "enable_trimming": cl.user_session.get("enable_trimming", True),
            # "recursion_limit": 50
        }),
    ):
        if isinstance(chunk, dict) and 'agent' in chunk:
            final_result = chunk
            usage = chunk['agent']['messages'][-1].additional_kwargs.get(
                'usage', {})
            token_counter.update_tokens(usage)

    await cl.Message(content=final_result['agent']['messages'][-1].content).send()

    if cl.user_session.get("show_token_count"):
        await cl.Message(
            content=token_counter.get_token_usage_content(),
            author="System (Token Usage)"
        ).send()
