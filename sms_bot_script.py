from asyncio.windows_events import NULL
import os
import sys
import logging
import uuid
from flask import Flask, request, jsonify
from google.cloud import dialogflowcx_v3 as dialogflow
import requests
import json
from supabase import create_client, Client


# --- LOGGING CONFIGURATION ---
log_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sms_bot_cx.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file_path),
        logging.StreamHandler(sys.stdout)
    ]
)

SUPABASE_URL = "secret"
SUPABASE_KEY = "secret"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

DIALOGFLOW_PROJECT_ID = "secret"
GOOGLE_APPLICATION_CREDENTIALS = "secret"

AGENT_ID = "secret"
LOCATION = "secret"
FLASK_PORT = 5000

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GOOGLE_APPLICATION_CREDENTIALS
app = Flask(__name__)

# FUNCTIONS

def log_to_supabase(sender, incoming_msg, outgoing_msg, phoneid):
    """Writes a record of the conversation to a Supabase table called 'conversations'."""
    try:
        supabase.table('conversations').insert({
            "sender": sender,
            "incoming_msg": incoming_msg,
            "outgoing_msg": outgoing_msg,
            "phoneid": phoneid,

        }).execute()
        
        logging.info(f"[SUPABASE LOG] Conversation logged: {sender} | {incoming_msg} | {outgoing_msg} | {phoneid}")
    except Exception as e:
        logging.error(f"[SUPABASE ERROR] Failed to log conversation: {e}")




def is_ai_active(phone_number):
    """Checks the 'ai_status' table to see if the AI should respond."""
    try:
        response = supabase.table('ai_status').select('is_active').eq('phone_number', phone_number).execute()
        # If there's data and the first result's 'is_active' is False, then AI is off.
        if response.data and response.data[0]['is_active'] is False:
            return False
    except Exception as e:
        logging.error(f"[SUPABASE CHECK ERROR] Could not check AI status: {e}")
        # If there's an error, default to AI being active to avoid blocking messages.
        return True
    
    # Default to AI being active if no specific rule is found.
    return True


def detect_intent_with_dialogflow_cx(text, session_id):
    """Sends a text query to Dialogflow CX and returns the fulfillment text."""
    logging.info(f"Connecting to Dialogflow CX agent: {AGENT_ID}...")
    try:
        client_options = {"api_endpoint": f"{LOCATION}-dialogflow.googleapis.com"}
        session_client = dialogflow.SessionsClient(client_options=client_options)
        session_path = session_client.session_path(
            project=DIALOGFLOW_PROJECT_ID, location=LOCATION, agent=AGENT_ID, session=session_id
        )
        
        text_input = dialogflow.TextInput(text=text)
        query_input = dialogflow.QueryInput(text=text_input, language_code="en") # Using 'en' for English
        
        request_obj = dialogflow.DetectIntentRequest(
            session=session_path, query_input=query_input
        )
        
        response = session_client.detect_intent(request=request_obj)
        response_messages = [" ".join(msg.text.text) for msg in response.query_result.response_messages]
        fulfillment_text = " ".join(response_messages)
        
        logging.info(f"  Intent: {response.query_result.match.intent.display_name}")
        logging.info(f"  Fulfillment Text: {fulfillment_text}")
        return fulfillment_text
    except Exception:
        logging.exception("[DIALOGFLOW CX ERROR] An exception occurred.")
        return "Sorry, my brain is having issues. Please try again later."


# flask webhook handler
@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"}), 200

@app.route('/sms', methods=['POST'])
def handle_incoming_sms():
    """Receives data from Automate, gets a reply, and returns it."""
    logging.info(">>> Webhook received from Automate!")
    webhook_data = request.get_json()
    sender_number = webhook_data.get("from")
    message_text = webhook_data.get("message")
    phoneid = webhook_data.get("id")


    if not sender_number or not message_text or not phoneid:
        logging.error(f"[WEBHOOK ERROR] Missing data: {webhook_data}")
        return jsonify({"error": "Missing 'from', 'message', or 'phoneid' field"}), 400


    logging.info(f"  From: {sender_number}\n  Message: '{message_text}'")
    
    if not is_ai_active(sender_number):
        logging.info(f"--- AI is STOPPED for {sender_number}. Logging incoming message and stopping. ---")
        # Log the incoming message with a note that the AI was stopped
        log_to_supabase(sender_number, message_text, "", phoneid)
        # Return an empty reply so the phone does nothing
        return jsonify({"reply": ""}), 200


    session_id = str(uuid.uuid4())
    reply_text = detect_intent_with_dialogflow_cx(message_text, session_id)
    
    if not reply_text:
        reply_text = "Sorry, I don't have a response for that."
        logging.warning("<<< No fulfillment text from Dialogflow CX. Sending default reply.")
    
    # Log the full conversation now that we have the AI reply
    log_to_supabase(sender_number, message_text, reply_text, phoneid)
    
    logging.info(f"<<< Sending response back to Automate: '{reply_text}'")
    
    # This JSON response is sent back to the Automate app
    return jsonify({"reply": reply_text}), 200


if __name__ == '__main__':
    logging.info("\n--- SMS Bot for Automate is starting ---")
    app.run(host='0.0.0.0', port=FLASK_PORT)