# Assistente do Canal

Projeto de automação para moderar o chat da Twitch por voz usando a Alexa.

A ideia é conseguir mandar mensagem no chat e dar/tirar timeout de alguém sem precisar parar o jogo durante a live. Os comandos não usam palavras-chave fixas: uma IA interpreta a frase falada e decide qual ação fazer.

Exemplos:

- "Alexa, peça para assistente do canal digitar bem-vindos à live"
- "Alexa, peça para assistente do canal banir o Everton por 10 minutos"
- "Alexa, peça para assistente do canal liberar o Everton"

## Como funciona

1. A Alexa reconhece a frase e envia o texto para a skill.
2. O código (Python, hospedado na AWS Lambda pela própria Alexa) busca na API da Twitch quem está no chat e quem está de timeout.
3. A frase e essas listas são enviadas para a IA (Groq), que devolve um JSON com a ação, o usuário e a duração.
4. O código faz algumas verificações e executa a ação na API da Twitch, ou pede confirmação por voz se tiver dúvida.

## Segurança

- A IA só interpreta a frase, quem executa é o código.
- Se a IA tiver pouca certeza ou o nome não bater com ninguém do chat, a Alexa pede confirmação.
- O código confere se o usuário escolhido foi realmente falado, para não punir a pessoa errada.
- Não tem ban permanente, só timeout com limite de 14 dias.
- As chaves ficam no `config.py`, que não vai para o repositório.

## Problemas encontrados

- O primeiro nome de chamada foi "modera live", mas a Alexa entendia "modern life", então foi trocado para "assistente do canal".
- A palavra "timeout" era reconhecida como "Darth Maul". Foram usados verbos em português (banir, liberar) e a IA foi ajustada para entender esses erros.
- "Silenciar" era entendido como comando de volume da própria Alexa.
- A Alexa escreve "um" como "1", o que fazia a mensagem sair como "1 oi gente".
- Quando a frase chegava sem nome, a IA escolhia outro usuário. Foi adicionada uma verificação no código para isso.

## Estrutura

- `lambda/lambda_function.py`: código da skill
- `lambda/config_exemplo.py`: modelo do arquivo de chaves
- `lambda/requirements.txt`: dependências
- `skill-package/interactionModels/custom/pt-BR.json`: modelo de voz da Alexa

## Como rodar

1. Criar uma skill Custom em Portuguese (BR), com hospedagem Alexa-hosted (Python), no console de desenvolvedor da Alexa.
2. Colar o `pt-BR.json` no JSON Editor do Interaction Model e fazer o Build.
3. Registrar um app em dev.twitch.tv/console e gerar o token com o Twitch CLI.
4. Criar uma chave no console.groq.com.
5. Copiar o `config_exemplo.py` para `config.py`, preencher as chaves, colar os arquivos da pasta `lambda` na aba Code e fazer o Deploy.

## Tecnologias

Alexa Skills Kit, Python, AWS Lambda, CloudWatch, API Helix da Twitch e Groq.
