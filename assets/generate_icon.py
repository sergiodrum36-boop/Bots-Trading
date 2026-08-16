"""Genera `assets/icon.ico` (icono de la app para la ventana, la bandeja
del sistema y el .exe -- ver Etapa 2 del Panel v2) a partir de formas
simples dibujadas con Pillow, en la paleta de marca ya confirmada
(fondo oscuro `#0F1320`, acento azul indigo `#5B8DEF`, verde suave
`#3FB68B` para el detalle del "trend").

No hace falta ningun archivo de imagen externo -- todo el diseno vive en
este script, asi que es facil ajustar colores/forma mas adelante sin
depender de un editor de imagenes: cambia las constantes de abajo y
vuelve a correr `python assets/generate_icon.py`.

Uso:
    pip install pillow   # si no esta ya instalado
    python assets/generate_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BACKGROUND = "#0F1320"
ACCENT = "#5B8DEF"
ACCENT_SOFT_GREEN = "#3FB68B"
CANVAS_SIZE = 256  # se generan tambien 16/32/48/128 dentro del .ico
CORNER_RADIUS_RATIO = 0.22  # esquinas redondeadas, look "app moderna"

ICON_SIZES = [16, 24, 32, 48, 64, 128, 256]

OUTPUT_PATH = Path(__file__).resolve().parent / "icon.ico"


def _draw_base(size: int) -> Image.Image:
    """Fondo oscuro con esquinas redondeadas, escalado a `size`."""
    scale = 4  # dibuja a mayor resolucion y reduce, para bordes mas suaves
    big = size * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    radius = int(big * CORNER_RADIUS_RATIO)
    draw.rounded_rectangle([0, 0, big - 1, big - 1], radius=radius, fill=BACKGROUND)
    return img, draw, big


def _draw_trend_glyph(draw: ImageDraw.ImageDraw, big: int) -> None:
    """Linea de tendencia ascendente (glifo simple tipo "bot de trading"):
    tres segmentos subiendo hacia la derecha, terminando en un punto --
    referencia directa a la curva de equity que es la pieza central del
    panel."""
    margin = big * 0.22
    points = [
        (margin, big - margin),
        (big * 0.42, big * 0.58),
        (big * 0.62, big * 0.72),
        (big - margin, margin),
    ]
    line_width = max(2, int(big * 0.07))
    draw.line(points, fill=ACCENT, width=line_width, joint="curve")

    # Puntas redondeadas en cada vertice, para que la linea no se vea
    # "cortada" en los angulos.
    cap_radius = line_width / 2
    for x, y in points:
        draw.ellipse(
            [x - cap_radius, y - cap_radius, x + cap_radius, y + cap_radius],
            fill=ACCENT,
        )

    # Punto de acento en el extremo superior (verde suave, el mismo tono
    # que usa el panel para P&L positivo) -- remata el glifo como si
    # fuera el valor actual en la curva de equity.
    end_x, end_y = points[-1]
    dot_radius = big * 0.06
    draw.ellipse(
        [end_x - dot_radius, end_y - dot_radius, end_x + dot_radius, end_y + dot_radius],
        fill=ACCENT_SOFT_GREEN,
    )


def build_icon() -> Image.Image:
    img, draw, big = _draw_base(CANVAS_SIZE)
    _draw_trend_glyph(draw, big)
    return img.resize((CANVAS_SIZE, CANVAS_SIZE), Image.LANCZOS)


def main() -> None:
    icon = build_icon()
    icon.save(OUTPUT_PATH, format="ICO", sizes=[(s, s) for s in ICON_SIZES])
    print(f"Icono generado: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
