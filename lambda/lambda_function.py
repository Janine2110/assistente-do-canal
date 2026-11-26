# Assistente do Canal - moderação do chat da Twitch por voz (Alexa + IA)
# Fluxo: Alexa -> IA interpreta a frase -> API da Twitch

import json
import logging
import re
import unicodedata
from difflib import SequenceMatcher
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.utils import is_intent_name, is_request_type

import config

log = logging.getLogger()
log.setLevel(logging.INFO)

# configurações
MODELO_IA = "openai/gpt-oss-120b"
CONFIANCA_MINIMA = 0.75  # abaixo disso pede confirmação
DURACAO_PADRAO = 60  # segundos
DURACAO_MAXIMA = 1209600  # 14 dias, limite da Twitch
MANTER_SESSAO_ABERTA = False

# a Alexa não manda a primeira palavra no slot, então cada intent devolve o verbo
VERBO_DO_INTENT = {
    "FalarIntent": "manda",
    "DarIntent": "dá",
    "TirarIntent": "tira",
    "BanirIntent": "banir",
    "DesbanirIntent": "desbanir",
    "ComandoIntent": "",
}

TWITCH_API = "https://api.twitch.tv/helix"
_token = {"access": config.TWITCH_TOKEN}
_meu_id = None


# funções da API da Twitch
def _renovar_token():
    # o token expira em algumas horas, aqui gera outro com o refresh token
    r = requests.post(
        "https://id.twitch.tv/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": config.TWITCH_REFRESH_TOKEN,
            "client_id": config.TWITCH_CLIENT_ID,
            "client_secret": config.TWITCH_CLIENT_SECRET,
        },
        timeout=3,
    )
    r.raise_for_status()
    _token["access"] = r.json()["access_token"]
    log.info("Token da Twitch renovado")


def twitch(metodo, caminho, params=None, corpo=None):
    for tentativa in range(2):
        r = requests.request(
            metodo,
            TWITCH_API + caminho,
            params=params,
            json=corpo,
            timeout=3,
            headers={
                "Authorization": f"Bearer {_token['access']}",
                "Client-Id": config.TWITCH_CLIENT_ID,
            },
        )
        if r.status_code == 401 and tentativa == 0:  # token vencido
            _renovar_token()
            continue
        if not r.ok:
            log.error("Twitch %s %s -> %s %s", metodo, caminho, r.status_code, r.text)
        r.raise_for_status()
        return r.json() if r.content else {}


def meu_id():
    global _meu_id
    if not _meu_id:
        _meu_id = twitch("GET", "/users")["data"][0]["id"]
    return _meu_id


def id_do_usuario(login):
    dados = twitch("GET", "/users", {"login": login})["data"]
    if not dados:
        raise ValueError(f"usuário {login} não existe")
    return dados[0]["id"]


def quem_esta_no_chat():
    try:
        d = twitch("GET", "/chat/chatters",
                   {"broadcaster_id": meu_id(), "moderator_id": meu_id(), "first": 1000})
        return [c["user_login"] for c in d["data"]]
    except Exception:
        log.exception("Não consegui listar o chat")
        return []


def quem_esta_punido():
    # usado pra IA saber quem é "ele" em "tira o timeout dele"
    try:
        d = twitch("GET", "/moderation/banned", {"broadcaster_id": meu_id(), "first": 100})
        itens = sorted(d["data"], key=lambda b: b["created_at"], reverse=True)
        return [{"usuario": b["user_login"], "expira": b["expires_at"] or "permanente"}
                for b in itens]
    except Exception:
        log.exception("Não consegui listar os punidos")
        return []


def mandar_mensagem(texto):
    d = twitch("POST", "/chat/messages", corpo={
        "broadcaster_id": meu_id(), "sender_id": meu_id(), "message": texto})
    resultado = d["data"][0]
    if not resultado.get("is_sent", True):
        raise RuntimeError(f"Twitch não enviou: {resultado.get('drop_reason')}")


def dar_timeout(login, segundos, motivo):
    twitch("POST", "/moderation/bans",
           {"broadcaster_id": meu_id(), "moderator_id": meu_id()},
           {"data": {"user_id": id_do_usuario(login), "duration": segundos,
                     "reason": motivo or "Timeout por voz"}})


def tirar_timeout(login):
    twitch("DELETE", "/moderation/bans",
           {"broadcaster_id": meu_id(), "moderator_id": meu_id(),
            "user_id": id_do_usuario(login)})


# IA (Groq) - transforma a frase em um JSON com a ação
PROMPT_IA = """Você interpreta comandos de voz de uma streamer da Twitch para moderar o próprio chat.
Você recebe um JSON com: "fala" (o que a Alexa entendeu), "ultimo_usuario" (último usuário citado
nesta conversa, pode ser null), "chat" (logins de quem está no chat agora) e "punidos" (quem está
de timeout/ban agora, do mais recente pro mais antigo).

Responda APENAS com um JSON, sem texto antes ou depois:
{"acao": "mensagem" | "timeout" | "remover_timeout" | "desconhecido",
 "usuario": "login exato" ou null,
 "mensagem": "texto" ou null,
 "duracao_segundos": número ou null,
 "motivo": "texto curto" ou null,
 "confianca": número de 0 a 1}

O que cada ação significa:
- "mensagem": ela quer que um texto apareça no chat, escrito pela conta dela.
- "timeout": ela quer silenciar alguém por um tempo. Pode vir como "dá timeout", "dá um timeout",
  "silencia", "cala a boca dele", "castiga", "deixa ele de castigo", "muta"...
- "banir", "bloquear", "expulsar", "suspender", "punir" também são "timeout" (este sistema não
  dá ban permanente; sem tempo falado, use null).
- "remover_timeout": ela quer liberar alguém que está de timeout. Pode vir como "tira o timeout",
  "tira o castigo", "libera", "desfaz", "perdoa", "desmuta", "desbanir", "desbloquear", "deixa ele falar de novo"...
- "desconhecido": só se realmente não der pra saber o que ela quer.

Como ler a fala:
- O reconhecimento de voz em português erra palavras em inglês. "timeout" pode chegar como
  "darth maul", "darth maut", "dar te maut", "tai maute", "tem auto", "time out", "taimaut"...
  Se parecer com "timeout", é timeout. "untimeout"/"desmuta" pode chegar parecido também.
- Palavras de educação não mudam nada: "pode", "por favor", "pra mim", "para mim", "aí", "agora".
- Comentários depois do comando ("ele é muito chato", "tá enchendo o saco") viram o "motivo".
- Nomes chegam escritos errado pelo reconhecimento de voz ("evertone" pode ser "everton",
  "pedro gamer" pode ser "pedrogamer_77"). Escolha o login mais parecido das listas "chat" e
  "punidos". Se nenhum for parecido, use o que ouviu, em minúsculas e sem espaços.
- "dele", "dela", "desse cara", "nele" = "ultimo_usuario"; se for null, o primeiro de "punidos".
- NUNCA invente usuário. Se ela não falou nenhum nome nem "dele/dela/desse", use "usuario": null.
- Duração: converta para segundos. "30 segundos" = 30, "1 minuto" = 60, "5 minutos" = 300,
  "10 minutos" = 600, "meia hora" = 1800, "1 hora" = 3600, "duas horas" = 7200,
  "1 dia" = 86400, "uma semana" = 604800. Sem duração falada: null.
- "agora" NÃO é duração: "tira o timeout do fulano agora" = tirar já.

Regras da mensagem:
- Copie o texto do jeito que ela falou, tirando só o comando ("manda", "fala", "escreve") e
  "no chat" do final.
- "um" e "uma" no começo da mensagem são artigo, NÃO número: "manda um oi" -> "oi".
  A Alexa costuma escrever esse artigo como "1": "manda 1 oi gente" -> "oi gente",
  "dá 1 castigo" = "dá um castigo".
- Só escreva dígitos quando ela claramente quer números: "manda um dois três" -> "123",
  "manda 123 no chat" -> "123".
- Não censure nem corrija o texto.

Confiança:
- 0.9 ou mais quando a ação está clara E o usuário (se tiver) está nas listas.
- 0.5 a 0.8 quando a ação está clara mas o nome não bate bem com ninguém das listas.
- Menos de 0.5 quando você está chutando a ação.

Exemplos:
fala "manda 1 oi gente chata" ->
{"acao":"mensagem","usuario":null,"mensagem":"oi gente chata","duracao_segundos":null,"motivo":null,"confianca":0.95}
fala "dá 1 castigo por 1 minuto" (nenhum nome falado) ->
{"acao":"timeout","usuario":null,"mensagem":null,"duracao_segundos":60,"motivo":null,"confianca":0.3}
fala "dá 1 castigo no everton" (chat tem "eevertone_") ->
{"acao":"timeout","usuario":"eevertone_","mensagem":null,"duracao_segundos":null,"motivo":null,"confianca":0.9}
fala "manda um oi gente" ->
{"acao":"mensagem","usuario":null,"mensagem":"oi gente","duracao_segundos":null,"motivo":null,"confianca":0.95}
fala "manda um dois três no chat" ->
{"acao":"mensagem","usuario":null,"mensagem":"123","duracao_segundos":null,"motivo":null,"confianca":0.95}
fala "pode dar um timeout no evertone pra mim ele é muito chato" (chat tem "everton") ->
{"acao":"timeout","usuario":"everton","mensagem":null,"duracao_segundos":null,"motivo":"muito chato","confianca":0.9}
fala "dá timeout no pedro gamer por 5 minutos" (chat tem "pedrogamer_77") ->
{"acao":"timeout","usuario":"pedrogamer_77","mensagem":null,"duracao_segundos":300,"motivo":null,"confianca":0.9}
fala "agora tira o timeout dele" (ultimo_usuario "everton") ->
{"acao":"remover_timeout","usuario":"everton","mensagem":null,"duracao_segundos":null,"motivo":null,"confianca":0.95}
fala "darth maul no everton de 10 minutos" ->
{"acao":"timeout","usuario":"everton","mensagem":null,"duracao_segundos":600,"motivo":null,"confianca":0.85}
fala "dá timeout no everton de 10 minutos" ->
{"acao":"timeout","usuario":"everton","mensagem":null,"duracao_segundos":600,"motivo":null,"confianca":0.95}
fala "silencia o everton por uma hora" ->
{"acao":"timeout","usuario":"everton","mensagem":null,"duracao_segundos":3600,"motivo":null,"confianca":0.95}
fala "tira o timeout do everton agora" ->
{"acao":"remover_timeout","usuario":"everton","mensagem":null,"duracao_segundos":null,"motivo":null,"confianca":0.95}
fala "banir o everton por um minuto" ->
{"acao":"timeout","usuario":"everton","mensagem":null,"duracao_segundos":60,"motivo":null,"confianca":0.95}
fala "desbanir o everton" ->
{"acao":"remover_timeout","usuario":"everton","mensagem":null,"duracao_segundos":null,"motivo":null,"confianca":0.95}
fala "libera o everton" ->
{"acao":"remover_timeout","usuario":"everton","mensagem":null,"duracao_segundos":null,"motivo":null,"confianca":0.9}
"""


def interpretar(fala, ultimo_usuario):
    inicio = time.time()
    # busca o chat e os punidos ao mesmo tempo pra ser mais rápido
    with ThreadPoolExecutor() as ex:
        f_chat, f_punidos = ex.submit(quem_esta_no_chat), ex.submit(quem_esta_punido)
        chat, punidos = f_chat.result(), f_punidos.result()

    log.info("Contexto da Twitch em %.1fs (%d no chat, %d punidos)",
             time.time() - inicio, len(chat), len(punidos))
    contexto = {"fala": fala, "ultimo_usuario": ultimo_usuario,
                "chat": chat[:300], "punidos": punidos[:30]}
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        timeout=4,
        headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
        json={"model": MODELO_IA,
              "reasoning_effort": "low",
              "response_format": {"type": "json_object"},
              "messages": [{"role": "system", "content": PROMPT_IA},
                           {"role": "user",
                            "content": json.dumps(contexto, ensure_ascii=False)}]},
    )
    if not r.ok:
        log.error("Groq -> %s %s", r.status_code, r.text)
    r.raise_for_status()
    texto = r.json()["choices"][0]["message"]["content"]
    log.info("IA respondeu em %.1fs no total", time.time() - inicio)
    acao = json.loads(re.search(r"\{.*\}", texto, re.S).group(0))

    return acao, set(chat), {p["usuario"] for p in punidos}


# verificações de segurança antes de executar
def _simplificar(texto):
    texto = unicodedata.normalize("NFKD", texto.lower())
    return re.sub(r"[^a-z]", "", texto.encode("ascii", "ignore").decode())


PRONOMES = ("dele", "dela", "nele", "nela", "esse", "essa", "desse", "dessa", "ele", "ela")


def nome_foi_falado(usuario, fala):
    # confere se o nome que a IA escolheu foi realmente falado (evita punir a pessoa errada)
    palavras = fala.lower().split()
    if any(p in PRONOMES for p in palavras):
        return True
    alvo = _simplificar(usuario or "")
    if not alvo:
        return False
    pedacos = palavras + [a + b for a, b in zip(palavras, palavras[1:])]
    return any(SequenceMatcher(None, alvo, _simplificar(p)).ratio() >= 0.6
               for p in pedacos if len(_simplificar(p)) >= 3)


def precisa_confirmar(acao, conhecidos, fala):
    confianca = float(acao.get("confianca") or 0)
    if acao["acao"] == "mensagem":
        return confianca < 0.5
    if confianca < CONFIANCA_MINIMA:
        return True
    if acao.get("usuario") not in conhecidos:
        return True
    if not nome_foi_falado(acao.get("usuario"), fala):
        return True
    return False


# texto que a Alexa fala
def falar_duracao(segundos):
    if segundos < 60:
        return f"{segundos} segundos"
    if segundos < 3600:
        m = round(segundos / 60)
        return "1 minuto" if m == 1 else f"{m} minutos"
    if segundos < 86400:
        h = round(segundos / 3600, 1)
        h = int(h) if h == int(h) else h
        return "1 hora" if h == 1 else f"{h} horas".replace(".", ",")
    d = round(segundos / 86400)
    return "1 dia" if d == 1 else f"{d} dias"


def duracao(acao):
    segundos = int(acao.get("duracao_segundos") or DURACAO_PADRAO)
    return max(1, min(segundos, DURACAO_MAXIMA))


def descrever(acao, feito=False):
    usuario = acao.get("usuario")
    if acao["acao"] == "mensagem":
        return f'mandei "{acao["mensagem"]}" no chat' if feito else f'mandar "{acao["mensagem"]}" no chat'
    if acao["acao"] == "timeout":
        tempo = falar_duracao(duracao(acao))
        return (f"{usuario} levou timeout de {tempo}" if feito
                else f"dar timeout de {tempo} em {usuario}")
    return f"tirei o timeout de {usuario}" if feito else f"tirar o timeout de {usuario}"


# executa a ação na Twitch
def executar(acao):
    if acao["acao"] == "mensagem":
        mandar_mensagem(acao["mensagem"])
    elif acao["acao"] == "timeout":
        dar_timeout(acao["usuario"], duracao(acao),
                    acao.get("motivo"))
    elif acao["acao"] == "remover_timeout":
        tirar_timeout(acao["usuario"])


# handlers da Alexa
sb = SkillBuilder()


def responder(h, fala, terminar=None):
    if terminar is None:
        terminar = not MANTER_SESSAO_ABERTA
    if terminar:
        return h.response_builder.speak(fala).set_should_end_session(True).response
    return perguntar(h, f"{fala} Mais alguma coisa?")


def perguntar(h, fala):
    # fala e deixa o microfone aberto
    return h.response_builder.speak(fala).ask(fala).response


# traduz os erros da Twitch pra uma resposta falada
def motivo_da_recusa(erro, acao):
    texto = ""
    if isinstance(erro, requests.HTTPError) and erro.response is not None:
        texto = erro.response.text.lower()
    texto += " " + str(erro).lower()
    usuario = acao.get("usuario") or "essa pessoa"
    if "already banned" in texto:
        return f"{usuario} já está de castigo."
    if "not banned" in texto:
        return f"{usuario} não está de castigo."
    if "duplicate" in texto:
        return "A Twitch não deixa mandar a mesma mensagem duas vezes seguidas."
    if "moderator" in texto or "may not be banned" in texto:
        return f"A Twitch não deixa punir {usuario}."
    if "não existe" in texto:
        return f"Não achei nenhum usuário chamado {usuario}."
    if "401" in texto or "token" in texto:
        return "Meu acesso à Twitch expirou. Precisa gerar um token novo."
    return "A Twitch recusou esse comando. Dá uma olhada nos logs."


def executar_e_responder(h, acao):
    try:
        executar(acao)
    except Exception as e:
        log.exception("Falha ao executar %s", acao)
        return responder(h, motivo_da_recusa(e, acao))
    if acao.get("usuario"):
        h.attributes_manager.session_attributes["ultimo_usuario"] = acao["usuario"]
    return responder(h, f"Pronto, {descrever(acao, feito=True)}.")


@sb.request_handler(can_handle_func=is_request_type("LaunchRequest"))
def abrir(h):
    return perguntar(h, "Assistente do canal pronta. O que você quer fazer?")


# comando principal: monta a frase, manda pra IA e decide se executa ou confirma
@sb.request_handler(can_handle_func=lambda h: any(is_intent_name(n)(h) for n in VERBO_DO_INTENT))
def comando(h):
    intent = h.request_envelope.request.intent
    slot = (intent.slots or {}).get("frase")
    fala = f"{VERBO_DO_INTENT[intent.name]} {slot.value if slot and slot.value else ''}".strip()
    sessao = h.attributes_manager.session_attributes

    try:
        acao, no_chat, punidos = interpretar(fala, sessao.get("ultimo_usuario"))
    except Exception:
        log.exception("Erro ao interpretar: %r", fala)
        return perguntar(h, "Deu um erro pra entender. Tenta de novo?")

    log.info("Fala: %r -> %s", fala, acao)
    if acao.get("acao") not in ("mensagem", "timeout", "remover_timeout"):
        return perguntar(h, "Não entendi o que você quer. Fala de outro jeito?")

    if acao["acao"] in ("timeout", "remover_timeout") and not acao.get("usuario"):
        return perguntar(h, "Não ouvi o nome da pessoa. Pode repetir o comando com o nome?")
    if acao["acao"] == "timeout" and acao["usuario"] in punidos:
        return responder(h, f"{acao['usuario']} já está de castigo.")
    if acao["acao"] == "remover_timeout" and acao["usuario"] in no_chat - punidos:
        return responder(h, f"{acao['usuario']} não está de castigo.")

    if precisa_confirmar(acao, no_chat | punidos, fala):
        sessao["pendente"] = acao
        return (h.response_builder
                .speak(f"Só confirmando: é pra {descrever(acao)}?")
                .ask("Sim ou não?").response)

    return executar_e_responder(h, acao)


@sb.request_handler(can_handle_func=is_intent_name("AMAZON.YesIntent"))
def sim(h):
    acao = h.attributes_manager.session_attributes.pop("pendente", None)
    if not acao:
        return responder(h, "Não tinha nada esperando confirmação.")
    return executar_e_responder(h, acao)


@sb.request_handler(can_handle_func=is_intent_name("AMAZON.NoIntent"))
def nao(h):
    h.attributes_manager.session_attributes.pop("pendente", None)
    return responder(h, "Beleza, cancelei.")


@sb.request_handler(can_handle_func=is_intent_name("AMAZON.HelpIntent"))
def ajuda(h):
    return perguntar(h, "Você pode dizer coisas como: manda oi no chat, dá timeout no fulano "
                        "por cinco minutos, ou tira o timeout dele.")


@sb.request_handler(can_handle_func=lambda h: is_intent_name("AMAZON.CancelIntent")(h)
                    or is_intent_name("AMAZON.StopIntent")(h))
def parar(h):
    return h.response_builder.speak("Até mais!").set_should_end_session(True).response


@sb.request_handler(can_handle_func=is_intent_name("AMAZON.FallbackIntent"))
def nao_entendeu(h):
    return perguntar(h, "Não peguei essa. Começa com manda, dá ou tira, por exemplo.")


@sb.request_handler(can_handle_func=is_request_type("SessionEndedRequest"))
def fim(h):
    return h.response_builder.response


@sb.exception_handler(can_handle_func=lambda h, e: True)
def erro(h, e):
    log.exception("Erro geral")
    return h.response_builder.speak("Ops, deu um erro aqui.").set_should_end_session(True).response


lambda_handler = sb.lambda_handler()
