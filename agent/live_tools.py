from agent.tools import (
	escalate,
	search_knowledge_base,
	save_collected_information,
	note_customer_query,
	send_verification_email,
	write_to_chat,
	end_call
)

# Map function names to the actual implementations
LIVE_TOOLS_MAP = {
	"escalate": escalate.invoke,
	"search_knowledge_base": search_knowledge_base.invoke,
	"save_collected_information": save_collected_information.invoke,
	"note_customer_query": note_customer_query.invoke,
	"send_verification_email": send_verification_email.invoke,
	"write_to_chat": write_to_chat.invoke,
	"end_call": end_call.invoke,
}

# Declarations formatted for Gemini's Multimodal Live API
LIVE_TOOL_DECLARATIONS = [
	{
		"name": "escalate",
		"description": "Use this tool when the user has a complaint or asks for a human. A customer service person will contact them later; no phone number is given.",
		"parameters": {
			"type": "OBJECT",
			"properties": {
				"reason": {
					"type": "STRING",
					"description": "The reason for the escalation."
				}
			},
			"required": ["reason"]
		}
	},
	{
		"name": "search_knowledge_base",
		"description": "Search the knowledge base about the MTB Islamic DPS on upay. Use this when the user asks about eligibility, plans, deposit amounts, tenure, profit, tax, withdrawal, early encashment, instalments, nominee or charges.",
		"parameters": {
			"type": "OBJECT",
			"properties": {
				"query": {
					"type": "STRING",
					"description": "The search query to look up in the knowledge base."
				}
			},
			"required": ["query"]
		}
	},
	{
		"name": "save_collected_information",
		"description": "Save details the user shares (Name, Mobile, Preferred Call Time, Interest) as {key: value}.",
		"parameters": {
			"type": "OBJECT",
			"properties": {
				"data": {
					"type": "OBJECT"
				}
			},
			"required": ["data"]
		}
	},
	{
		"name": "note_customer_query",
		"description": "Note a question you could not answer, a callback request (put the time in query) or a complaint, for customer service follow-up. One call per question.",
		"parameters": {
			"type": "OBJECT",
			"properties": {
				"query": {
					"type": "STRING",
					"description": "The customer's words, short."
				},
				"kind": {
					"type": "STRING",
					"description": "question | callback | complaint"
				}
			},
			"required": ["query"]
		}
	},
	{
		"name": "send_verification_email",
		"description": "Generate a temporary password (verification code) and send it to the user's email. Use this when a user provides their email to log in or verify their identity.",
		"parameters": {
			"type": "OBJECT",
			"properties": {
				"email": {
					"type": "STRING",
					"description": "The user's email address."
				}
			},
			"required": ["email"]
		}
	},
	{
		"name": "write_to_chat",
		"description": "Write a message directly to the text chat interface for the user to see. Use this when the user asks you to \"write it down\", \"spell it\", or provide detailed text (like a price list or link) during a voice call.",
		"parameters": {
			"type": "OBJECT",
			"properties": {
				"message": {
					"type": "STRING",
					"description": "The message text you want to output into the chatbox."
				}
			},
			"required": ["message"]
		}
	},
	{
		"name": "end_call",
		"description": "End the call, only after saying goodbye aloud. Always pass outcome.",
		"parameters": {
			"type": "OBJECT",
			"properties": {
				"outcome": {
					"type": "STRING",
					"description": "interested | not_interested | call_later | do_not_call | wrong_number | answered_questions | no_response"
				}
			}
		}
	}
]

def get_live_tools():
	"""Returns the tools formatted for Gemini Multimodal Live API"""
	return [{"functionDeclarations": LIVE_TOOL_DECLARATIONS}]
