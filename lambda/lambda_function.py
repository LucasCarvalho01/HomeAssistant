# -*- coding: utf-8 -*-
import warnings
import sys

if not sys.warnoptions:
    warnings.filterwarnings("ignore", category=SyntaxWarning)

import os
import re
import logging
import json
import random
import uuid
import requests
import requests.exceptions
import ask_sdk_core.utils as ask_utils

from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.dispatch_components import AbstractRequestHandler, AbstractExceptionHandler
from ask_sdk_model.interfaces.alexa.presentation.apl import RenderDocumentDirective, ExecuteCommandsDirective, OpenUrlCommand
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# Locale codes look like "pt-BR"; anything else falls back to en-US
LOCALE_PATTERN = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
# Only input_text entities are accepted for the prompter feature
INPUT_TEXT_PATTERN = re.compile(r"^input_text\.[a-z0-9_]+$")

# Load configurations and localization
def load_config(file_name):
    if str(file_name).endswith(".lang") and not os.path.exists(file_name):
        file_name = "locale/en-US.lang"
    try:
        with open(file_name, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or '=' not in line:
                    continue
                name, value = line.split('=', 1)
                # Store in global vars (tolerates spaces around "=")
                globals()[name.strip()] = value.strip()
    except Exception as e:
        logger.error(f"Error loading file: {str(e)}")

# Initial config load
load_config("config.cfg")
load_config("locale/en-US.lang")

# Log configuration
# bool("False") is True in Python, so compare the text explicitly
debug = str(globals().get("debug", "False")).strip().lower() == "true"
logger.setLevel(logging.DEBUG if debug else logging.INFO)

# Note: config.cfg stores values as strings; we'll normalize expected boolean
# flags to lowercase strings ("true"/"false") and compare explicitly.

# Validate HA settings
if not globals().get("home_assistant_url") or not globals().get("home_assistant_token"):
    raise ValueError("home_assistant_url or home_assistant_token configuration are not set!")

# Globals (configuration only; per-conversation state lives in session attributes)
last_interaction_date = None
user_locale = "US"  # Default locale
home_assistant_url = globals().get("home_assistant_url", "").strip().rstrip("/")
if not home_assistant_url.startswith("https://"):
    raise ValueError("home_assistant_url must start with https://")
apl_document_token = str(uuid.uuid4())
# Prompter feature is opt-in: empty means disabled
assist_input_entity = str(globals().get("assist_input_entity", "")).strip()
if assist_input_entity and not INPUT_TEXT_PATTERN.match(assist_input_entity):
    raise ValueError("assist_input_entity must look like input_text.some_name")
home_assistant_room_recognition = str(globals().get("home_assistant_room_recognition", "False")).lower()
home_assistant_kioskmode = str(globals().get("home_assistant_kioskmode", "False")).lower()
ask_for_further_commands = str(globals().get("ask_for_further_commands", "False")).lower()
suppress_greeting = str(globals().get("suppress_greeting", "False")).lower()

# Builds the headers for every call to Home Assistant
def ha_headers():
    headers = {
        "Authorization": "Bearer {}".format(globals().get("home_assistant_token")),
        "Content-Type": "application/json",
    }
    # Optional Cloudflare Access Service Token (both values must be set)
    cf_id = str(globals().get("cf_access_client_id", "")).strip()
    cf_secret = str(globals().get("cf_access_client_secret", "")).strip()
    if cf_id and cf_secret:
        headers["CF-Access-Client-Id"] = cf_id
        headers["CF-Access-Client-Secret"] = cf_secret
    return headers

# Logs a failed HA call without dumping secrets or whole HTML pages
def log_http_failure(context, response):
    if 300 <= response.status_code < 400:
        # Cloudflare Access answers 302 to its login page when the Service Token is missing or wrong
        logger.error(f"{context}: HTTP {response.status_code} redirect (check the Cloudflare Access Service Token/policy)")
    else:
        logger.error(f"{context}: HTTP {response.status_code} {response.text[:200]}")

# Checks, per request, whether the current device has a screen (APL)
def supports_apl(handler_input):
    device = handler_input.request_envelope.context.system.device
    return device.supported_interfaces.alexa_presentation_apl is not None

# Helper: fetch a prompt that Home Assistant left in an input_text helper
def fetch_prompt_from_ha():
    """
    Reads the state of your input_text helper via REST API and clears it,
    so the same prompt never runs twice. Disabled when assist_input_entity is empty.
    """
    if not assist_input_entity:
        return ""
    try:
        url = f"{home_assistant_url}/api/states/{assist_input_entity}"
        resp = requests.get(url, headers=ha_headers(), timeout=5, allow_redirects=False)
        if resp.status_code != 200:
            log_http_failure("HA state fetch failed", resp)
            return ""
        prompt = resp.json().get("state", "").strip()
        if prompt and prompt.lower() not in ("none", "unknown", "unavailable"):
            # Consume the prompt: reset the helper to "none"
            clear_url = f"{home_assistant_url}/api/services/input_text/set_value"
            clear = requests.post(
                clear_url,
                headers=ha_headers(),
                json={"entity_id": assist_input_entity, "value": "none"},
                timeout=3,
                allow_redirects=False,
            )
            if clear.status_code != 200:
                log_http_failure("HA prompt clear failed", clear)
            return prompt
    except Exception as e:
        logger.error(f"Error fetching prompt from HA state: {type(e).__name__}")
    return ""

def localize(handler_input):
    # Load locale per user
    locale = handler_input.request_envelope.request.locale or "en-US"
    if not LOCALE_PATTERN.match(locale):
        locale = "en-US"
    load_config(f"locale/{locale}.lang")

    # save user_locale var for regional differences in number handling like 2.4°C / 2,4°C
    global user_locale
    user_locale = locale.split("-")[1]  # "de-DE" -> "DE" split to respect lang differencies (not country specific)

class LaunchRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        global last_interaction_date

        localize(handler_input)

        # Check for a pre-set prompt from HA (only if the prompter feature is configured)
        prompt = fetch_prompt_from_ha()
        if prompt:
            # Process this prompt as user input and keep session open for follow-up
            response = process_conversation(prompt, handler_input)
            return handler_input.response_builder.speak(response).ask(globals().get("alexa_speak_question")).response

        # No prompt: if the device has a screen (APL support), loads the interface
        if supports_apl(handler_input):
            handler_input.response_builder.add_directive(
                RenderDocumentDirective(token=apl_document_token, document=load_template("apl_openha.json"))
            )
            
        # Sets the last access and defines which welcome phrase to respond to
        now = datetime.now(timezone(timedelta(hours=-3)))
        current_date = now.strftime('%Y-%m-%d')
        speak_output = globals().get("alexa_speak_next_message")
        if last_interaction_date != current_date:
            # First run of the day
            speak_output = globals().get("alexa_speak_welcome_message")
            last_interaction_date = current_date

        if suppress_greeting == "true":
            return handler_input.response_builder.ask("").response
        else:
            return handler_input.response_builder.speak(speak_output).ask(speak_output).response

class GptQueryIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("GptQueryIntent")(handler_input)

    def handle(self, handler_input):

        # Ensure locale is set correctly
        localize(handler_input)

        request = handler_input.request_envelope.request
        context = handler_input.request_envelope.context
        response_builder = handler_input.response_builder

        # Extract user query
        query = (request.intent.slots["query"].value or "").strip()
        logger.debug(f"Query received from Alexa: {query}")
        if not query:
            return response_builder.speak(globals().get("alexa_speak_help")).ask(globals().get("alexa_speak_help")).response

        # Handle keyword-based logic
        keyword_response = keywords_exec(query, handler_input)
        if keyword_response:
            return keyword_response

        # Include device ID if needed
        device_id = ""
        if home_assistant_room_recognition == "true":
            # Drop any spoken "device_id:" so only the real one reaches the LLM
            query = re.sub(r"device[_ ]?id\s*:?", "", query, flags=re.IGNORECASE).strip()
            device_id = f". device_id: {context.system.device.device_id}"

        full_query = query + device_id
        response = process_conversation(full_query, handler_input)
        logger.debug(f"Response generated: {response}")

        logger.debug(f"Ask for further commands enabled: {ask_for_further_commands}")
        if ask_for_further_commands == "true":
            return response_builder.speak(response).ask(globals().get("alexa_speak_question")).response
        else:
            return response_builder.speak(response).set_should_end_session(True).response

# Handles keywords to execute specific commands
def keywords_exec(query, handler_input):
    # Commands to open the dashboard
    keywords_top_open_dash = globals().get("keywords_to_open_dashboard").split(";")
    if any(ko.strip().lower() in query.lower() for ko in keywords_top_open_dash):
        logger.info("Opening Home Assistant dashboard")
        open_page(handler_input)
        return handler_input.response_builder.speak(globals().get("alexa_speak_open_dashboard")).response

    # Commands to close the skill — only if query has 3 or fewer words and matches closing keywords exactly
    keywords_close_skill = [k.strip().lower() for k in globals().get("keywords_to_close_skill").split(";")]
    query_words = query.lower().split()
    if len(query_words) <= 3:
        for kc in keywords_close_skill:
            # Match whole word or phrase using word boundaries to avoid substrings
            if re.search(r'\b' + re.escape(kc) + r'\b', query.lower()):
                logger.info("Closing skill from keyword command")
                return CancelOrStopIntentHandler().handle(handler_input)

    # If it is not a keyword or the context does not allow closing
    return None


# Calls the Home Assistant API and handles the response
def process_conversation(query, handler_input):
    # Conversation memory lives in the Alexa session: a one-shot request
    # ("pergunte ao ... X") starts a fresh conversation, an open session keeps it
    session_attr = handler_input.attributes_manager.session_attributes
    conversation_id = session_attr.get("conversation_id")

    # Gets user-configured environment variables
    if not home_assistant_url:
        logger.error("Please set 'home_assistant_url' in the config.cfg file.")
        return globals().get("alexa_speak_error")

    home_assistant_agent_id = globals().get("home_assistant_agent_id", None)
    home_assistant_language = globals().get("home_assistant_language", None)

    try:
        headers = ha_headers()
        data = {
            "text": replace_words(query)
        }
        # Adding optional parameters to request
        if home_assistant_language:
            data["language"] = home_assistant_language
        if home_assistant_agent_id:
            data["agent_id"] = home_assistant_agent_id
        if conversation_id:
            data["conversation_id"] = conversation_id

        ha_api_url = "{}/api/conversation/process".format(home_assistant_url)
        logger.debug(f"HA request url: {ha_api_url}")
        logger.debug(f"HA request data: {data}")

        # No redirects: a redirect here means Cloudflare Access rejected the request
        response = requests.post(ha_api_url, headers=headers, json=data, timeout=7, allow_redirects=False)

        logger.debug(f"HA response status: {response.status_code}")
        logger.debug(f"HA response data: {response.text}")

        contenttype = response.headers.get('Content-Type', '')
        logger.debug(f"Content-Type: {contenttype}")

        if response.status_code >= 300 and not contenttype.startswith("application/json"):
            log_http_failure("HA conversation call failed", response)
            return globals().get("alexa_speak_error")

        if contenttype.startswith("application/json"):
            response_data = response.json()
            speech = None

            if response.status_code == 200 and "response" in response_data:
                session_attr["conversation_id"] = response_data.get("conversation_id", conversation_id)
                response_type = response_data["response"]["response_type"]
                
                if response_type == "action_done" or response_type == "query_answer":
                    speech = response_data["response"]["speech"]["plain"]["speech"]
                    if "device_id:" in speech:
                        speech = speech.split("device_id:")[0].strip()
                elif response_type == "error":
                    speech = response_data["response"]["speech"]["plain"]["speech"]
                    logger.error(f"Error code: {response_data['response']['data']['code']}")
                else:
                    speech = globals().get("alexa_speak_error")

            if not speech:
                # Log HA's message, but never speak internal error text out loud
                if "message" in response_data:
                    logger.error(f"Empty speech (HTTP {response.status_code}): {response_data['message']}")
                else:
                    logger.error(f"Empty speech (HTTP {response.status_code})")
                return globals().get("alexa_speak_error")

            return improve_response(speech)
        else:
            log_http_failure("Unexpected HA response", response)
            return globals().get("alexa_speak_error")

    except requests.exceptions.Timeout:
        logger.error("Timeout when communicating with Home Assistant")
        return globals().get("alexa_speak_timeout")

    except Exception as e:
        logger.error(f"Error processing response: {type(e).__name__}", exc_info=debug)
        return globals().get("alexa_speak_error")

# Replaces incorrectly generated words by Alexa interpreter in the query
def replace_words(query):
    query = query.replace('4.º','quarto')
    return query

# Replaces words and special characters to improve API response speech
def improve_response(speech):
    global user_locale
    speech = speech.replace(':\n\n', '').replace('\n\n', '. ').replace('\n', ',').replace('-', '').replace('_', ' ')

    # Change decimal separator if user_locale = "de-DE"
    if user_locale == "DE":
        # Only replace decimal separators and not 1.000 separators
        speech = re.sub(r'(\d+)\.(\d{1,3})(?!\d)', r'\1,\2', speech)  # Decimal point (e.g. 2.4 -> 2,4)
    
    speech = re.sub(r'[^A-Za-z0-9çÇáàâãäéèêíïóôõöúüñÁÀÂÃÄÉÈÊÍÏÓÔÕÖÚÜÑ\sß.,!?°]', '', speech)
    return speech

# Loads the initial APL screen template
def load_template(filepath):
    with open(filepath, encoding='utf-8') as f:
        template = json.load(f)

    if filepath == 'apl_openha.json':
        # Locate dynamic texts in the APL
        template['mainTemplate']['items'][0]['items'][2]['text'] = globals().get("echo_screen_welcome_text")
        template['mainTemplate']['items'][0]['items'][3]['text'] = globals().get("echo_screen_click_text")
        template['mainTemplate']['items'][0]['items'][4]['onPress']['source'] = get_hadash_url()
        template['mainTemplate']['items'][0]['items'][4]['item']['text'] = globals().get("echo_screen_button_text")

    return template

# Opens Home Assistant dashboard in Silk browser
def open_page(handler_input):
    if supports_apl(handler_input):
        # Renders an empty template, required for the OpenURL command
        # https://amazon.developer.forums.answerhub.com/questions/220506/alexa-open-a-browser.html
        
        handler_input.response_builder.add_directive(
            RenderDocumentDirective(
                token=apl_document_token,
                document=load_template("apl_empty.json")
            )
        )
        
        # Open default page of dashboard
        handler_input.response_builder.add_directive(
            ExecuteCommandsDirective(
                token=apl_document_token,
                commands=[OpenUrlCommand(source=get_hadash_url())]
            )
        )

# Builds the Home Assistant dashboard URL
def get_hadash_url():
    ha_dashboard_url = home_assistant_url
    ha_dashboard_url += "/{}".format(globals().get("home_assistant_dashboard", "lovelace"))
    
    
    if home_assistant_kioskmode == "true":
        ha_dashboard_url += '?kiosk'
    
    logger.debug(f"ha_dashboard_url: {ha_dashboard_url}")
    return ha_dashboard_url

class HelpIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("AMAZON.HelpIntent")(handler_input)

    def handle(self, handler_input):
        speak_output = globals().get("alexa_speak_help")
        return handler_input.response_builder.speak(speak_output).ask(speak_output).response

class CancelOrStopIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("AMAZON.CancelIntent")(handler_input) or ask_utils.is_intent_name("AMAZON.StopIntent")(handler_input)

    def handle(self, handler_input):
        speak_output = random.choice(globals().get("alexa_speak_exit").split(";"))
        return handler_input.response_builder.speak(speak_output).set_should_end_session(True).response

class SessionEndedRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_request_type("SessionEndedRequest")(handler_input)

    def handle(self, handler_input):
        return handler_input.response_builder.response

class CanFulfillIntentRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_request_type("CanFulfillIntentRequest")(handler_input)

    def handle(self, handler_input):
        localize(handler_input)
        
        intent_name = handler_input.request_envelope.request.intent.name if handler_input.request_envelope.request.intent else None
        if intent_name == "GptQueryIntent":
            return handler_input.response_builder.can_fulfill("YES").add_can_fulfill_intent("YES").response
        else:
            return handler_input.response_builder.can_fulfill("NO").add_can_fulfill_intent("NO").response

class CatchAllExceptionHandler(AbstractExceptionHandler):
    def can_handle(self, handler_input, exception):
        return True

    def handle(self, handler_input, exception):
        logger.error(f"Unhandled exception: {type(exception).__name__}", exc_info=debug)
        speak_output = globals().get("alexa_speak_error")
        return handler_input.response_builder.speak(speak_output).ask(speak_output).response

sb = SkillBuilder()
# Optional: only accept requests from this skill (set alexa_skill_id in config.cfg)
alexa_skill_id = str(globals().get("alexa_skill_id", "")).strip()
if alexa_skill_id:
    sb.skill_id = alexa_skill_id
sb.add_request_handler(LaunchRequestHandler())
sb.add_request_handler(GptQueryIntentHandler())
sb.add_request_handler(HelpIntentHandler())
sb.add_request_handler(CancelOrStopIntentHandler())
sb.add_request_handler(SessionEndedRequestHandler())
sb.add_request_handler(CanFulfillIntentRequestHandler())
sb.add_exception_handler(CatchAllExceptionHandler())
lambda_handler = sb.lambda_handler()
