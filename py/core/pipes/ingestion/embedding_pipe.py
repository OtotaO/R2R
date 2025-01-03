import asyncio
import logging
from typing import Any, AsyncGenerator

from core.base import (
    AsyncState,
    DocumentChunk,
    EmbeddingProvider,
    R2RDocumentProcessingError,
    Vector,
    VectorEntry,
)
from core.base.pipes.base_pipe import AsyncPipe

logger = logging.getLogger()


class EmbeddingPipe(AsyncPipe[VectorEntry]):
    """
    Embeds extractions using a specified embedding model.
    """

    class Input(AsyncPipe.Input):
        message: list[DocumentChunk]

    def __init__(
        self,
        embedding_provider: EmbeddingProvider,
        config: AsyncPipe.PipeConfig,
        embedding_batch_size: int = 1,
        *args,
        **kwargs,
    ):
        super().__init__(config)
        self.embedding_provider = embedding_provider
        self.embedding_batch_size = embedding_batch_size

    async def embed(self, extractions: list[DocumentChunk]) -> list[float]:
        return await self.embedding_provider.async_get_embeddings(
            [extraction.data for extraction in extractions],  # type: ignore
            EmbeddingProvider.PipeStage.BASE,
        )

    async def _process_batch(
        self, extraction_batch: list[DocumentChunk]
    ) -> list[VectorEntry]:
        vectors = await self.embed(extraction_batch)
        return [
            VectorEntry(
                id=extraction.id,
                document_id=extraction.document_id,
                owner_id=extraction.owner_id,
                collection_ids=extraction.collection_ids,
                vector=Vector(data=raw_vector),
                text=extraction.data,  # type: ignore
                metadata={
                    **extraction.metadata,
                },
            )
            for raw_vector, extraction in zip(vectors, extraction_batch)
        ]

    async def _run_logic(  # type: ignore
        self,
        input: AsyncPipe.Input,
        state: AsyncState,
        run_id: Any,
        *args: Any,
        **kwargs: Any,
    ) -> AsyncGenerator[VectorEntry, None]:
        if not isinstance(input, EmbeddingPipe.Input):
            raise ValueError(
                f"Invalid input type for embedding pipe: {type(input)}"
            )
        extraction_batch = []
        batch_size = self.embedding_batch_size
        concurrent_limit = (
            self.embedding_provider.config.concurrent_request_limit
        )
        tasks = set()

        async def process_batch(batch):
            return await self._process_batch(batch)

        try:
            for item in input.message:
                extraction_batch.append(item)

                if len(extraction_batch) >= batch_size:
                    tasks.add(
                        asyncio.create_task(process_batch(extraction_batch))
                    )
                    extraction_batch = []

                while len(tasks) >= concurrent_limit:
                    done, tasks = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        for vector_entry in await task:
                            yield vector_entry

            if extraction_batch:
                tasks.add(asyncio.create_task(process_batch(extraction_batch)))

            for future_task in asyncio.as_completed(tasks):
                for vector_entry in await future_task:
                    yield vector_entry
        finally:
            # Ensure all tasks are completed
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_extraction(
        self, extraction: DocumentChunk
    ) -> VectorEntry | R2RDocumentProcessingError:
        try:
            if isinstance(extraction.data, bytes):
                raise ValueError(
                    "extraction data is in bytes format, which is not supported by the embedding provider."
                )

            vectors = await self.embedding_provider.async_get_embeddings(
                [extraction.data],
                EmbeddingProvider.PipeStage.BASE,
            )

            return VectorEntry(
                id=extraction.id,
                document_id=extraction.document_id,
                owner_id=extraction.owner_id,
                collection_ids=extraction.collection_ids,
                vector=Vector(data=vectors[0]),
                text=extraction.data,
                metadata={**extraction.metadata},
            )
        except Exception as e:
            logger.error(f"Error processing extraction: {e}")
            return R2RDocumentProcessingError(
                error_message=str(e),
                document_id=extraction.document_id,
            )
