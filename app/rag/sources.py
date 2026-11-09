"""What goes into the knowledge base.

Two complementary strategies (both through Tavily):
1. CRAWL the official sites from their entry points (breadth).
2. TOPIC DISCOVERY: targeted site-restricted searches for the topics customers ask about most,
   then EXTRACT those pages (depth). This guards against the crawler missing deep product pages.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class CrawlSource:
    url: str
    max_depth: int = 2
    limit: int = 60


CRAWL_SOURCES = [
    CrawlSource("https://www.getnet.net/en", max_depth=2, limit=40),
    CrawlSource("https://www.getnet.com.br", max_depth=2, limit=80),
]

ALLOWED_DOMAINS = ["getnet.net", "getnet.com.br"]

# Topics that mirror the evaluation scenarios and real customer FAQs.
TOPIC_QUERIES = [
    "Get Clássica maquininha",
    "Get Smart maquininha",
    "diferença Get Clássica e Get Smart",
    "Pix Getnet receber vendas conta bancária",
    "antecipação de recebíveis Getnet",
    "prazo de recebimento vendas Getnet",
    "Link de Pagamento Getnet WhatsApp",
    "crediário parcelamento Getnet número de parcelas",
    "maquininha sem conexão internet o que fazer",
    "transação negada recusada maquininha Getnet",
    "taxas Getnet maquininha",
    "Getnet receivables advance",
    "Getnet card machine plans",
    "Getnet Pix payments",
]
