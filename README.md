# SmartRoutine Kiosk (Cliente + Cozinha + Dono + Caixa)

Aplicação exemplo de **autoatendimento** para lanchonete, com:
- **Cliente (kiosk):** menu, carrinho e checkout + **acompanhamento do pedido**
- **Cozinha:** recebe pedidos e muda status
- **Dono:** painel financeiro por mês (Receita **paga** x Despesas) + lista de pedidos
- **Caixa:** tela de cobrança com **PIX / Cartão / Dinheiro**

## Rodar local

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

pip install -r requirements.txt
python app.py
```

Rotas:
- Cliente: http://127.0.0.1:5000/cliente/menu
- Acompanhar: http://127.0.0.1:5000/cliente/pedido
- Cozinha: http://127.0.0.1:5000/login/cozinha (PIN padrão: 1234)
- Dono: http://127.0.0.1:5000/login/dono (PIN padrão: 9999)

## Observações importantes
- A receita do painel do dono considera **apenas pedidos pagos** (registrados no Caixa).
- Se você já rodou uma versão anterior, a aplicação tenta adicionar automaticamente as novas colunas no SQLite.
  Se der algum erro, apague o arquivo `instance/app.db` e rode novamente (vai recriar e seedar).
# SmartRoutine-Kiosk
