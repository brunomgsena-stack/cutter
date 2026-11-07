# describe.py <img1> <img2> ... : descreve cada quadro com um modelo de visao local (MLX da Apple).
# Roda no Python 3.12 do ambiente isolado do Cutter (~/Library/Application Support/Cutter/vlm).
# Para cada imagem imprime um marcador \x01VLM\x01<caminho> e, na linha seguinte, a descricao.
import os
import sys

from mlx_vlm import generate, load
from mlx_vlm.prompt_utils import apply_chat_template

MODEL = os.environ.get("CUTTER_VLM_MODEL", "mlx-community/Qwen2.5-VL-3B-Instruct-4bit")
Q = ("Descreva em portugues, em uma frase curta e objetiva, a cena desta imagem: "
     "pessoas, objetos, acoes e ambiente. Nao comente a qualidade nem diga que e uma imagem.")

model, processor = load(MODEL)
config = model.config
for img in sys.argv[1:]:
    sys.stdout.write("\x01VLM\x01" + img + "\n")
    sys.stdout.flush()
    try:
        prompt = apply_chat_template(processor, config, Q, num_images=1)
        out = generate(model, processor, prompt, image=[img], max_tokens=90,
                       temperature=0.0, verbose=False)
        txt = out.text if hasattr(out, "text") else out
        sys.stdout.write(str(txt).strip().replace("\n", " ") + "\n")
    except Exception:
        sys.stdout.write("\n")
    sys.stdout.flush()
