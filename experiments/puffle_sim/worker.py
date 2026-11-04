"""Single-request simulator. It has no credentials, tools or network code."""
import json
import sys


def main():
    request = json.loads(sys.stdin.buffer.readline(16385))
    messages = request.get('context', [])
    latest = next((text for role, text in reversed(messages) if role == 'player'), '')
    answer = '¡Hola! ¿Jugamos?' if '?' in latest else ''
    print(json.dumps({'reply': bool(answer), 'text': answer}, ensure_ascii=True))


if __name__ == '__main__':
    main()
