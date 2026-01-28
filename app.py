from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from openai import OpenAI
import os, json, tempfile, requests
from docx import Document
from datetime import datetime
from bs4 import BeautifulSoup

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# OpenAI client (make sure OPENAI_API_KEY is set in hosting env vars)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# -----------------------------------------------------------
# 🔧 Helper Functions (each fetches one top item)
# -----------------------------------------------------------

def fetch_price_ace(item_keywords: str):
    try:
        url = f"https://www.acehardware.ph/search?q={requests.utils.quote(item_keywords)}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        card = soup.select_one(".product-card")
        if not card:
            return None

        title = card.select_one(".product-name").get_text(strip=True) if card.select_one(".product-name") else item_keywords
        price_el = card.select_one(".price")

        if not price_el:
            return None

        price_text = price_el.get_text(strip=True)
        price = float(price_text.replace("₱", "").replace(",", "").split()[0])

        return {
            "store": f"ACE Hardware – {title}",
            "address": "Various ACE branches in Metro Manila",
            "unit_price": price,
            "confidence": "high",
            "source": "ACE"
        }

    except Exception as e:
        print("fetch_price_ace error:", e)
        return None


def fetch_price_wilcon(item_keywords: str):
    try:
        url = f"https://shop.wilcon.com.ph/search?q={requests.utils.quote(item_keywords)}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        card = soup.select_one(".product-card")
        if not card:
            return None

        title = card.select_one(".product-title").get_text(strip=True) if card.select_one(".product-title") else item_keywords
        price_el = card.select_one(".price")

        if not price_el:
            return None

        price_text = price_el.get_text(strip=True)
        price = float(price_text.replace("₱", "").replace(",", "").split()[0])

        return {
            "store": f"Wilcon Depot – {title}",
            "address": "Various Wilcon branches in Metro Manila",
            "unit_price": price,
            "confidence": "high",
            "source": "Wilcon"
        }

    except Exception as e:
        print("fetch_price_wilcon error:", e)
        return None


def fetch_price_shopee(item_keywords: str):
    try:
        url = (
            "https://shopee.ph/api/v4/search/search_items"
            f"?by=relevancy&keyword={requests.utils.quote(item_keywords)}&limit=1"
        )

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json",
            "Referer": "https://shopee.ph/",
        }

        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if not data.get("items"):
            return None

        item = data["items"][0]["item_basic"]
        name = item.get("name", item_keywords)

        # Shopee price is usually stored as integer with 100000 divisor
        price = item.get("price", 0) / 100000

        if price <= 0:
            return None

        return {
            "store": f"Shopee – {name[:60]}",
            "address": "Online / Philippines",
            "unit_price": price,
            "confidence": "high",
            "source": "Shopee"
        }

    except Exception as e:
        print("fetch_price_shopee error:", e)
        return None


# -----------------------------------------------------------
# 🧠 Main Store Fetcher (Always 3 total items max)
# -----------------------------------------------------------

@app.route("/get-stores", methods=["POST"])
def get_stores():
    try:
        data = request.get_json(silent=True) or {}
        item = (data.get("item") or "").strip()
        quantity = int(data.get("quantity") or 1)

        if not item:
            return jsonify({"error": "Item is required"}), 400

        results = []

        # Removed Lazada Selenium because it fails on most free hosting
        for fetch_func in [fetch_price_wilcon, fetch_price_ace, fetch_price_shopee]:
            res = fetch_func(item)
            if res:
                res["quantity"] = quantity
                res["total_price"] = float(res["unit_price"]) * quantity
                results.append(res)

            if len(results) >= 3:
                break

        # AI fallback if fewer than 3 results
        if len(results) < 3:
            print("[DEBUG] AI fallback for missing stores...")

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that returns valid JSON only."},
                    {"role": "user", "content": f"""
Return ONLY valid JSON array like:
[
  {{
    "store": "Store Name",
    "address": "Full address in Manila",
    "price": 123.45,
    "confidence": "high"
  }}
]

Find Manila stores selling: {item}
Return up to 3 items.
"""}
                ]
            )

            ai_str = (response.choices[0].message.content or "").strip()

            # Safer JSON extraction
            start = ai_str.find("[")
            end = ai_str.rfind("]")
            if start != -1 and end != -1 and end > start:
                json_str = ai_str[start:end + 1]
                ai_stores = json.loads(json_str)
            else:
                ai_stores = []

            for s in ai_stores:
                if len(results) >= 3:
                    break

                try:
                    price = float(s.get("price", 0))
                except:
                    price = 0

                if price <= 0:
                    continue

                results.append({
                    "store": s.get("store", "Unknown Store"),
                    "address": s.get("address", "Manila"),
                    "unit_price": price,
                    "quantity": quantity,
                    "total_price": price * quantity,
                    "confidence": s.get("confidence", "medium"),
                    "source": "AI"
                })

        results = results[:3]
        return jsonify({"stores": results})

    except Exception as e:
        print("Error in /get-stores:", e)
        return jsonify({"error": str(e)}), 500


# -----------------------------------------------------------
# 🧾 Purchase Request
# -----------------------------------------------------------

@app.route("/submit-pr", methods=["POST"])
def submit_pr():
    try:
        # Accept JSON OR FormData
        if request.is_json:
            data = request.get_json(silent=True) or {}
        else:
            data = json.loads(request.form.get("data", "{}"))

        item = data.get("item", "")
        description = data.get("description", "")
        purpose = data.get("purpose", "")
        quantity = data.get("quantity", "")
        stores = data.get("stores", [])
        template_name = data.get("template_name", "template1.docx")

        template_path = os.path.join(os.getcwd(), template_name)
        if not os.path.exists(template_path):
            return jsonify({"error": f"Template not found: {template_name}"}), 400

        doc = Document(template_path)
        today = datetime.now().strftime("%B %d, %Y")

        for p in doc.paragraphs:
            text = p.text

            text = text.replace("{{item}}", str(item))
            text = text.replace("{{description}}", str(description))
            text = text.replace("{{purpose}}", str(purpose))
            text = text.replace("{{quantity}}", str(quantity))
            text = text.replace("{{date}}", today)

            for i, s in enumerate(stores, start=1):
                text = text.replace(f"{{{{store{i}}}}}", str(s.get("store", "")))
                text = text.replace(f"{{{{address{i}}}}}", str(s.get("address", "")))
                text = text.replace(f"{{{{quantity{i}}}}}", str(s.get("quantity", "")))
                text = text.replace(f"{{{{unit{i}}}}}", f"₱{float(s.get('unit_price', 0)):,.2f}")
                text = text.replace(f"{{{{total{i}}}}}", f"₱{float(s.get('total_price', 0)):,.2f}")

            if text != p.text:
                p.text = text

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".docx")
        doc.save(tmp.name)
        tmp.close()

        return send_file(tmp.name, as_attachment=True, download_name="Purchase_Request.docx")

    except Exception as e:
        print("Error in /submit-pr:", e)
        return jsonify({"error": str(e)}), 500


# -----------------------------------------------------------
# 🟢 Health Check
# -----------------------------------------------------------

@app.route("/ping")
def ping():
    return {"status": "ok"}


@app.after_request
def after_request(response):
    response.headers.add("Access-Control-Allow-Origin", "*")
    response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
    response.headers.add("Access-Control-Allow-Methods", "GET,PUT,POST,DELETE,OPTIONS")
    return response

@app.route("/generate-questions", methods=["POST"])
def generate_questions():
    try:
        data = request.get_json(silent=True) or {}
        description = data.get("description", "").strip()

        if not description:
            return jsonify({"error": "Description is required"}), 400

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You generate survey questions in JSON format."
                },
                {
                    "role": "user",
                    "content": f"""
Return ONLY valid JSON like this:
[
  {{
    "question": "Question text",
    "type": "short answer | multiple choice | dropdown | linear",
    "options": ["Option 1", "Option 2"]
  }}
]

Generate 5 survey questions based on:
{description}
"""
                }
            ]
        )

        ai_text = response.choices[0].message.content.strip()

        start = ai_text.find("[")
        end = ai_text.rfind("]")

        if start == -1 or end == -1:
            return jsonify({"questions": []})

        questions = json.loads(ai_text[start:end + 1])

        return jsonify({"questions": questions})

    except Exception as e:
        print("generate-questions error:", e)
        return jsonify({"error": str(e)}), 500



# -----------------------------------------------------------
# 🚀 Run locally / deploy
# -----------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
