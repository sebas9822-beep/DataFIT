"""Modelo de curaduría: normaliza, clasifica, enlaza duplicados y mide la calidad de lo capturado.

Primero corren las reglas (deterministas y reproducibles). Claude interviene después, solo sobre
lo que las reglas dejan dudoso, y cada cambio suyo se valida y queda en la bitácora.
"""
