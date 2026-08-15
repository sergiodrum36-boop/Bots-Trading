"""Script de prueba rapida: comprueba que tus credenciales de Alpaca
funcionan y que el conector puede leer datos reales del mercado.

No envia ninguna orden ni hace ninguna operacion -- solo lee informacion
publica de mercado (si el mercado esta abierto y la ultima vela de SPY).

Uso:
    python probar_conexion.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from connectors.alpaca_connector import AlpacaConnector  # noqa: E402
from connectors.base_connector import ConnectorAuthenticationError, ConnectorError  # noqa: E402


def main() -> None:
    print("Conectando con Alpaca (paper trading)...")

    try:
        connector = AlpacaConnector(paper=True)
    except ConnectorAuthenticationError as exc:
        print(f"\nERROR de credenciales: {exc}")
        print(
            "Revisa el archivo .env: debe tener APCA_API_KEY_ID y "
            "APCA_API_SECRET_KEY rellenos con tus claves de PAPER TRADING."
        )
        return

    try:
        abierto = connector.is_market_open()
        print(f"Mercado abierto ahora mismo: {abierto}")

        ultima_vela = connector.get_latest_bar("SPY")
        print(
            f"Ultima vela de SPY -> cierre: {ultima_vela.close}, "
            f"hora: {ultima_vela.timestamp}"
        )
        print("\n¡Conexion con Alpaca funcionando correctamente!")
    except ConnectorError as exc:
        print(f"\nERROR consultando datos de mercado: {exc}")
        print(
            "Si el error menciona 401/403, revisa que las claves esten bien "
            "copiadas en .env (sin espacios ni comillas de mas)."
        )


if __name__ == "__main__":
    main()
