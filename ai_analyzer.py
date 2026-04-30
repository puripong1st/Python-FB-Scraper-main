"""
ai_analyzer.py
━━━━━━━━━━━━━━
ClaudeAnalyzer — ส่งข้อความข่าวให้ Claude วิเคราะห์ คืนผลเป็น dict
"""

import json
import re
import anthropic


class ClaudeAnalyzer:
    def __init__(self, api_key: str, system_prompt: str, log_callback):
        self.api_key       = api_key
        self.system_prompt = system_prompt
        self.log           = log_callback
        self.client        = anthropic.Anthropic(api_key=api_key) if api_key else None

    def analyze(self, post_text: str) -> dict | None:
        if not self.client:
            return None

        prompt = f"""{self.system_prompt}

ข้อความข่าว:
{post_text}
"""
        try:
            response = self.client.messages.create(
                model="claude-3-5-sonnet-20240620",
                max_tokens=800,
                temperature=0.1,
                messages=[
                    {"role": "user",      "content": prompt},
                    {"role": "assistant", "content": "{"},  # บังคับให้เริ่มตอบด้วย JSON ทันที
                ]
            )
            result_text = "{" + response.content[0].text

            # ล้างโค้ด markdown เผื่อ AI ตอบกลับมาติด ```json
            match = re.search(r'\{.*\}', result_text, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            return json.loads(result_text)

        except Exception as e:
            self.log(f"❌ AI Analysis Error: {e}")
            return None
