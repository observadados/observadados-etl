import os
import sys
import requests
import smtplib
import json
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

def _load_env():
    # Helper para garantir que as variáveis de ambiente do mailer estão carregadas
    from pathlib import Path
    env_path = Path('.env')
    if env_path.exists():
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    k, v = line.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip())

def send_admin_email(published_items: list[dict[str, object]]) -> None:
    """Envia um e-mail de resumo para o administrador do WordPress quando novos arquivos são publicados."""
    if not published_items:
        return
        
    _load_env()
    wp_url = os.environ.get('WP_URL')
    wp_user = os.environ.get('WP_USER')
    wp_pass = os.environ.get('WP_APP_PASSWORD')

    # Tenta enviar via WordPress REST API primeiro, aproveitando a conexão Gmail OAuth do WP Mail SMTP
    if wp_url and wp_user and wp_pass:
        print("📧 Solicitando envio de e-mail via API do WordPress (WP Mail SMTP)...", end=' ', file=sys.stderr, flush=True)
        try:
            api_send_report = f"{wp_url.rstrip('/')}/wp-json/observadados/v1/send-report"
            payload = {'published_items': published_items}
            response = requests.post(
                api_send_report,
                auth=(wp_user, wp_pass),
                json=payload,
                timeout=30
            )
            if response.status_code in [200, 201]:
                print("OK (e-mail enviado via WordPress / WP Mail SMTP)", file=sys.stderr)
                return
            else:
                print(f"\n⚠️ Falha no endpoint do WordPress ({response.status_code}: {response.text}). Tentando SMTP legado...", file=sys.stderr)
        except Exception as e:
            print(f"\n⚠️ Erro de conexão com a API do WordPress ({e}). Tentando SMTP legado...", file=sys.stderr)

    # Fallback para SMTP legado se as credenciais do WP estiverem ausentes ou se a requisição REST falhar
    smtp_server = os.environ.get('SMTP_SERVER')
    smtp_port = os.environ.get('SMTP_PORT')
    smtp_user = os.environ.get('SMTP_USER')
    smtp_pass = os.environ.get('SMTP_PASSWORD')
    smtp_from = os.environ.get('SMTP_FROM') or smtp_user
    admin_email = os.environ.get('ADMIN_EMAIL')

    if not smtp_server or not smtp_user or not smtp_pass or not admin_email:
        print("⚠️ Ignorando envio de e-mail: configurações de SMTP e WordPress API ausentes ou incompletas no arquivo .env.", file=sys.stderr)
        return

    try:
        port = int(smtp_port) if smtp_port else 587
    except ValueError:
        port = 587

    print(f"📧 Enviando e-mail de resumo para {admin_email} via SMTP legado...", end=' ', file=sys.stderr, flush=True)

    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"🔔 ObservaDados: Relatório de Publicação - {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    msg['From'] = smtp_from
    msg['To'] = admin_email

    # Construção de tabela HTML elegante
    rows_html = []
    for item in published_items:
        action_color = '#3182ce' if item['action'] == 'Atualizado' else '#319795'
        status_color = '#e53e3e' if item['status'] == 'draft' else '#2f855a'
        wp_url_env = os.environ.get('WP_URL', '').rstrip('/')
        admin_link = f"{wp_url_env}/wp-admin/post.php?post={item['wp_post_id']}&action=edit" if wp_url_env else '#'
        
        rows_html.append(f"""
        <tr style="border-bottom: 1px solid #dddddd;">
            <td style="padding: 12px 15px;">{item['collector_key']}</td>
            <td style="padding: 12px 15px;">{item['uf']}</td>
            <td style="padding: 12px 15px;">{item.get('filename', '-')}</td>
            <td style="padding: 12px 15px; font-weight: bold; color: {action_color};">{item['action']}</td>
            <td style="padding: 12px 15px; font-weight: bold; color: {status_color};">{str(item['status']).upper()}</td>
            <td style="padding: 12px 15px;"><a href="{admin_link}" style="color: #3182ce; text-decoration: none;">ID {item['wp_post_id']}</a></td>
        </tr>
        """)

    html_content = f"""
    <html>
    <body style="font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f7fafc; color: #2d3748; padding: 20px; margin: 0;">
        <div style="max-width: 800px; margin: 0 auto; background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1); border-top: 5px solid #3182ce;">
            <div style="background-color: #2b6cb0; padding: 30px; text-align: center; color: white;">
                <h1 style="margin: 0; font-size: 24px; font-weight: 600; letter-spacing: 0.5px;">ObservaDados - Coletores</h1>
                <p style="margin: 5px 0 0 0; opacity: 0.9; font-size: 14px;">Relatório Automático de Coleta e Publicação (ETL Refatorado)</p>
            </div>
            <div style="padding: 30px;">
                <p style="font-size: 16px; line-height: 1.6; margin-top: 0;">Olá, Administrador,</p>
                <p style="font-size: 16px; line-height: 1.6;">O processo de coleta automática foi executado e os seguintes datasets foram processados no WordPress:</p>
                
                <div style="margin-top: 25px; margin-bottom: 25px; overflow-x: auto;">
                    <table style="border-collapse: collapse; width: 100%; text-align: left; font-size: 14px; min-width: 600px;">
                        <thead>
                            <tr style="background-color: #f7fafc; border-bottom: 2px solid #e2e8f0; color: #4a5568;">
                                <th style="padding: 12px 15px; font-weight: 600;">Coletor</th>
                                <th style="padding: 12px 15px; font-weight: 600;">UF</th>
                                <th style="padding: 12px 15px; font-weight: 600;">Arquivo</th>
                                <th style="padding: 12px 15px; font-weight: 600;">Ação</th>
                                <th style="padding: 12px 15px; font-weight: 600;">Status do Post</th>
                                <th style="padding: 12px 15px; font-weight: 600;">Link Admin</th>
                            </tr>
                        </thead>
                        <tbody>
                            {"".join(rows_html)}
                        </tbody>
                    </table>
                </div>
                
                <p style="font-size: 14px; color: #718096; line-height: 1.6;">Nota: Os datasets marcados como <strong>DRAFT</strong> (Rascunho) foram cadastrados pela primeira vez e requerem revisão e publicação manual pelo administrador.</p>
            </div>
        </div>
    </body>
    </html>
    """

    msg.attach(MIMEText(html_content, 'html'))

    try:
        if port == 465:
            server = smtplib.SMTP_SSL(smtp_server, port, timeout=30)
        else:
            server = smtplib.SMTP(smtp_server, port, timeout=30)
            server.ehlo()
            server.starttls()
            server.ehlo()

        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_from, [admin_email], msg.as_string())
        server.quit()
        print("OK (e-mail enviado com sucesso)", file=sys.stderr)
    except Exception as e:
        print(f"ERRO ao enviar e-mail: {e}", file=sys.stderr)
