import base64
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add parent directory to sys.path so config and app can be imported
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app
import config


class TestFlaskWebApp(unittest.TestCase):
    def setUp(self):
        app.app.config['TESTING'] = True
        self.client = app.app.test_client()

    # ── Static File Tests ────────────────────────────────────────────────────
    def test_index_route(self):
        res = self.client.get('/')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Legal Copilot BD', res.data)
        self.assertIn(b"API_BASE = 'api'", res.data)

    def test_index_html_route(self):
        res = self.client.get('/index.html')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Legal Copilot BD', res.data)

    def test_scorces_css_route(self):
        res = self.client.get('/scorces/style.css')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'root', res.data)

    def test_live_scorces_css_route(self):
        res = self.client.get('/live/scorces/style.css')
        self.assertEqual(res.status_code, 200)

    def test_not_found_route(self):
        res = self.client.get('/scorces/non_existent.css')
        self.assertEqual(res.status_code, 404)

    # ── CORS & OPTIONS Tests ─────────────────────────────────────────────────
    def test_options_api_php(self):
        res = self.client.options('/api.php')
        self.assertEqual(res.status_code, 204)
        self.assertEqual(res.headers.get('Access-Control-Allow-Origin'), '*')
        self.assertIn('POST', res.headers.get('Access-Control-Allow-Methods', ''))

    def test_options_api(self):
        res = self.client.options('/api')
        self.assertEqual(res.status_code, 204)
        self.assertEqual(res.headers.get('Access-Control-Allow-Origin'), '*')

    # ── Validation Tests ─────────────────────────────────────────────────────
    def test_empty_question(self):
        res = self.client.post('/api.php', data={'question': ''})
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertEqual(data.get('error'), 'Question is required')

    def test_empty_question_api_route(self):
        res = self.client.post('/api', data={'question': ''})
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertEqual(data.get('error'), 'Question is required')

    def test_long_question(self):
        res = self.client.post('/api.php', data={'question': 'a' * 4001})
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertEqual(data.get('error'), 'Question too long (max 4000 chars)')

    # ── Notice Keyword Detection Tests ───────────────────────────────────────
    def test_is_notice_related(self):
        self.assertTrue(app.is_notice_related("What is the latest notice?"))
        self.assertTrue(app.is_notice_related("When is the next exam routine?"))
        self.assertTrue(app.is_notice_related("How to check semester result and cgpa?"))
        self.assertTrue(app.is_notice_related("Upcoming university seminar"))
        self.assertFalse(app.is_notice_related("How do I write a binary search in Python?"))
        self.assertFalse(app.is_notice_related("Tell me a funny joke"))

    # ── General Mode Mocked Test ─────────────────────────────────────────────
    @patch('app.call_groq')
    def test_general_chat_mode(self, mock_groq):
        mock_groq.return_value = {'answer': 'Daffodil International University is in Dhaka.'}
        res = self.client.post(
            '/api.php',
            data={
                'question': 'Tell me about DIU',
                'mode': 'general',
                'history': json.dumps([{'role': 'user', 'content': 'Hi'}, {'role': 'assistant', 'content': 'Hello!'}]),
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data['mode'], 'general')
        self.assertIn('Daffodil International University', data['answer'])
        self.assertEqual(data['sources'], [])
        self.assertEqual(data['notices'], [])
        self.assertIsInstance(data['related'], list)

    # ── RAG Mode Mocked Tests ────────────────────────────────────────────────
    @patch('requests.post')
    @patch('app.call_groq')
    def test_rag_mode_offline_fallback(self, mock_groq, mock_post):
        mock_post.side_effect = Exception("Connection refused")
        mock_groq.return_value = {'answer': 'General knowledge answer'}

        res = self.client.post(
            '/api.php',
            data={
                'question': 'What are the rules of admission?',
                'mode': 'rag',
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data['mode'], 'rag')
        self.assertIn('RAG server unavailable', data['answer'])
        self.assertIn('General knowledge answer', data['answer'])

    @patch('requests.post')
    def test_rag_mode_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'answer': 'According to the university policy, attendance must be 75%.',
            'sources': [{
                'source': 'Handbook.pdf',
                'page': 12,
                'url': 'http://diu.edu.bd/handbook.pdf',
                'snippet': 'Attendance requirement is 75%',
                'cited': True,
            }],
            'related': ['Can attendance be waived?'],
            'model_name': 'GPT-OSS 120B',
        }
        mock_post.return_value = mock_resp

        res = self.client.post(
            '/api.php',
            data={
                'question': 'What is the attendance requirement?',
                'mode': 'rag',
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data['mode'], 'rag')
        self.assertIn('attendance must be 75%', data['answer'])
        self.assertEqual(len(data['sources']), 1)
        self.assertEqual(data['sources'][0]['title'], 'Handbook.pdf · p.12')

    # ── Web Mode Mocked Test ─────────────────────────────────────────────────
    @patch('app.search_via_ddg')
    @patch('app.call_groq')
    def test_web_mode(self, mock_groq, mock_ddg):
        mock_ddg.return_value = [{
            'title': 'DIU News',
            'url': 'https://diu.edu.bd/news/1',
            'snippet': 'Campus news updates',
        }]
        mock_groq.return_value = {'answer': 'Recent DIU news [1]'}

        res = self.client.post(
            '/api.php',
            data={
                'question': 'Latest sports news at DIU',
                'mode': 'web',
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data['mode'], 'web')
        self.assertIn('Recent DIU news', data['answer'])
        self.assertEqual(len(data['sources']), 1)
        self.assertEqual(data['sources'][0]['favicon'], '🎓')


    # ── Notice Mode Mocked Test ──────────────────────────────────────────────
    @patch('app.scrape_notices')
    @patch('app.call_groq')
    def test_notice_mode(self, mock_groq, mock_scrape):
        mock_scrape.return_value = [{
            'title': 'Midterm Examination Routine Spring 2026',
            'url': 'https://noticeboard.diu.edu.bd/notice/123',
            'date': '2026-03-15',
            'category': 'Exam',
            'summary': 'Midterm exam starts from March 20',
        }]
        mock_groq.return_value = {'answer': 'Here is the **Midterm Examination Routine**.'}

        res = self.client.post(
            '/api.php',
            data={
                'question': 'When is the midterm exam schedule?',
                'mode': 'general',  # Notice detection should auto-switch to notice mode
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data['mode'], 'notice')
        self.assertIn('Midterm Examination Routine', data['answer'])
        self.assertEqual(len(data['notices']), 1)
        self.assertEqual(data['sources'][0]['title'], 'DIU Notice Board')

    # ── TTS Endpoint Tests ───────────────────────────────────────────────────
    def test_tts_empty_text(self):
        res = self.client.post('/api/tts', json={'text': ''})
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertEqual(data.get('error'), 'Text is required')

    def test_tts_missing_gemini_key_fallback(self):
        with patch.object(config, 'GEMINI_API_KEY', ''):
            res = self.client.post('/api/tts', json={'text': 'Hello world'})
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data.get('fallback'))
            self.assertIn('GEMINI_API_KEY', data.get('error', ''))

    @patch('requests.post')
    def test_tts_gemini_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        # Dummy 16-bit PCM bytes
        dummy_pcm = b'\x00\x00' * 100
        mock_resp.json.return_value = {
            'candidates': [{
                'content': {
                    'parts': [{
                        'inlineData': {
                            'mimeType': 'audio/pcm;rate=24000',
                            'data': base64.b64encode(dummy_pcm).decode('ascii')
                        }
                    }]
                }
            }]
        }
        mock_post.return_value = mock_resp

        with patch.object(config, 'GEMINI_API_KEY', 'fake_gemini_key'):
            res = self.client.post('/api/tts', json={'text': 'The Penal Code of Bangladesh', 'gender': 'female'})
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get('source'), 'gemini')
            self.assertEqual(data.get('mime_type'), 'audio/wav')
            self.assertTrue(len(data.get('audio_base64', '')) > 0)


if __name__ == '__main__':
    unittest.main()
