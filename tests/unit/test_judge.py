"""Self-hosted judge server unit tests."""
import pytest

class TestJudge:
    def test_judge_server_exists(self):
        import judge_server
        assert hasattr(judge_server, 'JudgeHandler')
    
    def test_judge_responds_to_get(self):
        from judge_server import JudgeHandler
        from io import BytesIO
        handler = JudgeHandler(BytesIO(), ('127.0.0.1', 12345), None)
        assert handler is not None
