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


## Acompanhamento por CPF
- No checkout, o cliente informa CPF.
- O sistema **não guarda o CPF puro**, apenas o **hash** + últimos 4 dígitos.
- Para acompanhar, o cliente informa o CPF e vê apenas pedidos vinculados a ele.

- Validação do CPF: inclui verificação dos dígitos (DV).
- Botão 'Trocar CPF' limpa a sessão para evitar acesso por terceiros no mesmo dispositivo.


## Pagamentos com AbacatePay

Este projeto agora suporta pagamento online com AbacatePay no checkout do cliente.

### O que foi adicionado
- opção de pagar no caixa ou online
- checkout online com PIX e cartão via AbacatePay
- salvamento do link e do status do pagamento no pedido
- sincronização automática do status ao abrir a tela do pedido
- webhook `/webhooks/abacatepay` para confirmação automática

### Variáveis de ambiente
Configure estas variáveis:

- `ABACATEPAY_API_KEY`: chave da API criada no dashboard
- `ABACATEPAY_WEBHOOK_SECRET`: secret definido no webhook do dashboard

### Configuração do webhook no dashboard
Cadastre a URL abaixo na AbacatePay:

`https://SEU-DOMINIO/webhooks/abacatepay?webhookSecret=SEU_SECRET`

### Fluxo
1. Cliente escolhe “Pagar online com AbacatePay”
2. O sistema cria o pedido interno
3. O sistema chama a API da AbacatePay e recebe a URL do checkout
4. Cliente paga com PIX ou cartão
5. A AbacatePay redireciona o cliente de volta e/ou envia webhook
6. O pedido fica marcado como pago
