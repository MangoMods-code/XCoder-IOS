import os

from loguru import logger
from sc_compression import compress

from xcoder.localization import locale


def compress_csv(
    folder: str = "./CSV/In-Decompressed",
    folder_export: str = "./CSV/Out-Compressed",
):
    from sc_compression.signatures import Signatures


    for file in os.listdir(folder):
        if file.endswith(".csv"):
            try:
                with open(f"{folder}/{file}", "rb") as f:
                    file_data = f.read()

                with open(f"{folder_export}/{file}", "wb") as f:
                    f.write(compress(file_data, Signatures.LZMA))
            except Exception as exception:
                logger.exception(
                    locale.error
                    % (
                        exception.__class__.__module__,
                        exception.__class__.__name__,
                        exception,
                    )
                )

            print()
