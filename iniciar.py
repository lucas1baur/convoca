#!/usr/bin/env python3
"""Inicia o Convoca localmente e abre o navegador."""
import webbrowser, threading, time
import uvicorn

def abrir():
    time.sleep(1.5)
    webbrowser.open("http://127.0.0.1:8000")

if __name__ == "__main__":
    print("Convoca rodando em http://127.0.0.1:8000  (Ctrl+C para encerrar)")
    threading.Thread(target=abrir, daemon=True).start()
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000)
