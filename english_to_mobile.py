import requests
from appium import webdriver
from appium.options.android import UiAutomator2Options

# Use your Perplexity API key
PPLX_API_KEY = "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"

headers = {
    "Authorization": f"Bearer {PPLX_API_KEY}",
    "Content-Type": "application/json"
}

# Perplexity API endpoint (please check documentation, example below)
api_url = "https://api.perplexity.ai/v1/chat/completions"

def get_pplx_response(prompt):
    payload = {
        "model": "pplx-70b-chat",            # or another model listed in their docs
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 256,
        "temperature": 0
    }
    response = requests.post(api_url, headers=headers, json=payload)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]

# Create an options object and set capabilities as attributes
caps = UiAutomator2Options()
caps.platformName = "Android"
caps.deviceName = "emulator-5554"       # Change as needed
caps.appPackage = "com.manastik.dadt"         # Replace with your app package
caps.appActivity = "com.manastik.dadt.MainActivity"  # Replace with your app's main activity
caps.noReset = True

# Initialize the Appium driver by passing options (not caps dict)
driver = webdriver.Remote("http://localhost:4723", options=caps)


eng_instr = input("Enter English command for the mobile app: ")
prompt = f"""
You are an assistant that converts English instructions into Python Appium commands using the `driver` variable.
Instruction: "{eng_instr}"
Provide only the sequence of Python Appium commands (no explanations).
"""

appium_code = get_pplx_response(prompt)
print("\nAI-generated Python Appium commands:\n")
print(appium_code)

# Uncomment below only after manually reviewing AI output for safety
# exec(appium_code)
