import os
from typing import Any, Dict, List, Optional

import requests
from llama_index import (
    QueryBundle,
    ServiceContext,
    StorageContext,
    load_index_from_storage,
)
from llama_index.callbacks import CallbackManager, CBEventType, EventPayload
from llama_index.core.base_retriever import BaseRetriever
from llama_index.postprocessor import CohereRerank
from llama_index.postprocessor.types import BaseNodePostprocessor
from llama_index.query_engine import RetrieverQueryEngine
from llama_index.response_synthesizers import BaseSynthesizer, ResponseMode
from llama_index.retrievers import BM25Retriever
from llama_index.schema import NodeWithScore, QueryType, TextNode
from llama_index.vector_stores import FaissVectorStore
from llama_index.vector_stores.simple import DEFAULT_VECTOR_STORE, NAMESPACE_SEP
from llama_index.vector_stores.types import DEFAULT_PERSIST_FNAME
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

import wandb
from wandbot.utils import (
    create_no_result_dummy_node,
    get_logger,
    load_service_context,
)

logger = get_logger(__name__)


class LanguageFilterPostprocessor(BaseNodePostprocessor):
    """Language-based Node processor."""

    languages: List[str] = ["en", "python"]
    min_result_size: int = 10

    @classmethod
    def class_name(cls) -> str:
        return "LanguageFilterPostprocessor"

    def _postprocess_nodes(
        self,
        nodes: List[NodeWithScore],
        query_bundle: Optional[QueryBundle] = None,
    ) -> List[NodeWithScore]:
        """Postprocess nodes."""

        new_nodes = []
        for node in nodes:
            if node.metadata["language"] in self.languages:
                new_nodes.append(node)

        if len(new_nodes) < self.min_result_size:
            return new_nodes + nodes[: self.min_result_size - len(new_nodes)]

        return new_nodes


class MetadataPostprocessor(BaseNodePostprocessor):
    """Metadata-based Node processor."""

    min_result_size: int = 10
    include_tags: List[str] | None = None
    exclude_tags: List[str] | None = None

    @classmethod
    def class_name(cls) -> str:
        return "MetadataPostprocessor"

    def _postprocess_nodes(
        self,
        nodes: List[NodeWithScore],
        query_bundle: Optional[QueryBundle] = None,
    ) -> List[NodeWithScore]:
        """Postprocess nodes."""
        if not self.include_tags and not self.exclude_tags:
            return nodes
        new_nodes = []
        for node in nodes:
            normalized_tags = [
                tag.lower().strip() for tag in node.metadata["tags"]
            ]
            if self.include_tags:
                normalized_include_tags = [
                    tag.lower().strip() for tag in self.include_tags
                ]
                if not set(normalized_include_tags).issubset(
                    set(normalized_tags)
                ):
                    continue
            if self.exclude_tags:
                normalized_exclude_tags = [
                    tag.lower().strip() for tag in self.exclude_tags
                ]
                if set(normalized_exclude_tags).issubset(set(normalized_tags)):
                    continue
            new_nodes.append(node)
        if len(new_nodes) < self.min_result_size:
            dummy_node = create_no_result_dummy_node()
            new_nodes.extend(
                [dummy_node] * (self.min_result_size - len(new_nodes))
            )
        return new_nodes


class YouRetriever(BaseRetriever):
    """You retriever."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        similarity_top_k: int = 10,
        callback_manager: Optional[CallbackManager] = None,
    ) -> None:
        """Init params."""
        self._api_key = api_key or os.environ["YOU_API_KEY"]
        self.similarity_top_k = (
            similarity_top_k if similarity_top_k <= 20 else 20
        )
        super().__init__(callback_manager)

    def _retrieve(self, query_bundle: QueryBundle) -> List[NodeWithScore]:
        """Retrieve."""
        try:
            headers = {"X-API-Key": self._api_key}
            url = "https://api.ydc-index.io/search"

            querystring = {
                "query": "Weights & Biases, W&B, wandb or Weave "
                + query_bundle.query_str,
                "num_web_results": self.similarity_top_k,
            }
            response = requests.get(url, headers=headers, params=querystring)
            if response.status_code != 200:
                return []
            else:
                results = response.json()

            snippets = [hit["snippets"] for hit in results["hits"]]
            snippet_metadata = [
                {
                    "source": hit["url"],
                    "language": "en",
                    "description": hit["description"],
                    "title": hit["title"],
                    "tags": ["you.com"],
                }
                for hit in results["hits"]
            ]
            search_hits = []
            for snippet_list, metadata in zip(snippets, snippet_metadata):
                for snippet in snippet_list:
                    search_hits.append((snippet, metadata))

            return [
                NodeWithScore(
                    node=TextNode(text=s[0], metadata=s[1]),
                    score=1.0,
                )
                for s in search_hits
            ]
        except Exception as e:
            return []


class HybridRetriever(BaseRetriever):
    def __init__(
        self,
        index,
        storage_context,
        similarity_top_k: int = 20,
    ):
        self.index = index
        self.storage_context = storage_context

        self.vector_retriever = self.index.as_retriever(
            similarity_top_k=similarity_top_k,
            storage_context=self.storage_context,
        )
        self.bm25_retriever = BM25Retriever.from_defaults(
            docstore=self.index.docstore,
            similarity_top_k=similarity_top_k,
        )
        self.you_retriever = YouRetriever(
            api_key=os.environ.get("YOU_API_KEY"),
            similarity_top_k=similarity_top_k,
        )
        super().__init__()

    def _retrieve(self, query: QueryBundle, **kwargs):
        bm25_nodes = self.bm25_retriever.retrieve(query)
        vector_nodes = self.vector_retriever.retrieve(query)
        you_nodes = (
            self.you_retriever.retrieve(query)
            if not kwargs.get("is_avoid_query", False)
            else []
        )

        # combine the two lists of nodes
        all_nodes = []
        node_ids = set()
        for n in bm25_nodes + vector_nodes + you_nodes:
            if n.node.node_id not in node_ids:
                all_nodes.append(n)
                node_ids.add(n.node.node_id)
        return all_nodes

    def retrieve(
        self, str_or_query_bundle: QueryType, **kwargs
    ) -> List[NodeWithScore]:
        self._check_callback_manager()

        if isinstance(str_or_query_bundle, str):
            query_bundle = QueryBundle(str_or_query_bundle)
        else:
            query_bundle = str_or_query_bundle
        with self.callback_manager.as_trace("query"):
            with self.callback_manager.event(
                CBEventType.RETRIEVE,
                payload={EventPayload.QUERY_STR: query_bundle.query_str},
            ) as retrieve_event:
                nodes = self._retrieve(query_bundle, **kwargs)
                retrieve_event.on_end(
                    payload={EventPayload.NODES: nodes},
                )
        return nodes


class RetrieverConfig(BaseSettings):
    index_artifact: str = Field(
        "wandbot/wandbot-dev/wandbot_index:latest",
        env="WANDB_INDEX_ARTIFACT",
        validation_alias="wandb_index_artifact",
    )
    embeddings_model: str = "text-embedding-3-small"
    embeddings_size: int = 512
    top_k: int = Field(
        default=10,
        env="RETRIEVER_TOP_K",
    )
    similarity_top_k: int = Field(
        default=10,
        env="RETRIEVER_SIMILARITY_TOP_K",
    )
    language: str = Field(
        default="en",
        env="RETRIEVER_LANGUAGE",
    )
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="allow"
    )


class WandbRetrieverQueryEngine(RetrieverQueryEngine):
    def __init__(
        self,
        retriever: HybridRetriever,
        response_synthesizer: Optional[BaseSynthesizer] = None,
        node_postprocessors: Optional[List[BaseNodePostprocessor]] = None,
        callback_manager: Optional[CallbackManager] = None,
    ) -> None:
        super().__init__(
            retriever=retriever,
            response_synthesizer=response_synthesizer,
            node_postprocessors=node_postprocessors,
            callback_manager=callback_manager,
        )

    def retrieve(
        self, query_bundle: QueryBundle, **kwargs
    ) -> List[NodeWithScore]:
        nodes = self._retriever.retrieve(query_bundle, **kwargs)
        return self._apply_node_postprocessors(nodes, query_bundle=query_bundle)


class Retriever:
    def __init__(
        self,
        config: RetrieverConfig | None = None,
        run: wandb.wandb_sdk.wandb_run.Run | None = None,
        service_context: ServiceContext | None = None,
        callback_manager: CallbackManager | None = None,
    ):
        self.config = (
            config if isinstance(config, RetrieverConfig) else RetrieverConfig()
        )
        self.run = run
        self.service_context = (
            service_context
            if service_context
            else load_service_context(
                embeddings_model=self.config.embeddings_model,
                embeddings_size=self.config.embeddings_dim,
                callback_manager=callback_manager,
            )
        )

        self.storage_context = self.load_storage_context_from_artifact(
            artifact_url=self.config.index_artifact
        )

        self.index = load_index_from_storage(
            self.storage_context, service_context=self.service_context
        )
        self._retriever = HybridRetriever(
            index=self.index,
            similarity_top_k=self.config.similarity_top_k,
            storage_context=self.storage_context,
        )
        self.is_avoid_query: bool | None = None

    def load_storage_context_from_artifact(
        self, artifact_url: str
    ) -> StorageContext:
        """Loads the storage context from the given artifact URL.

        Args:
            artifact_url: A string representing the URL of the artifact.

        Returns:
            An instance of StorageContext.
        """
        artifact = self.run.use_artifact(artifact_url)
        artifact_dir = artifact.download()
        index_path = f"{artifact_dir}/{DEFAULT_VECTOR_STORE}{NAMESPACE_SEP}{DEFAULT_PERSIST_FNAME}"
        logger.debug(f"Loading index from {index_path}")
        storage_context = StorageContext.from_defaults(
            vector_store=FaissVectorStore.from_persist_path(index_path),
            persist_dir=artifact_dir,
        )
        return storage_context

    def load_query_engine(
        self,
        top_k: int | None = None,
        language: str | None = None,
        include_tags: List[str] | None = None,
        exclude_tags: List[str] | None = None,
        is_avoid_query: bool | None = None,
    ) -> WandbRetrieverQueryEngine:
        top_k = top_k or self.config.top_k
        language = language or self.config.language

        if is_avoid_query is not None:
            self.is_avoid_query = is_avoid_query

        node_postprocessors = [
            MetadataPostprocessor(
                include_tags=include_tags,
                exclude_tags=exclude_tags,
                min_result_size=top_k,
            ),
            LanguageFilterPostprocessor(
                languages=[language, "python"], min_result_size=top_k
            ),
            CohereRerank(top_n=top_k, model="rerank-english-v2.0")
            if language == "en"
            else CohereRerank(top_n=top_k, model="rerank-multilingual-v2.0"),
        ]
        query_engine = WandbRetrieverQueryEngine.from_args(
            retriever=self._retriever,
            node_postprocessors=node_postprocessors,
            response_mode=ResponseMode.NO_TEXT,
            service_context=self.service_context,
        )
        return query_engine

    def retrieve(
        self,
        query: str,
        language: str | None = None,
        top_k: int | None = None,
        include_tags: List[str] | None = None,
        exclude_tags: List[str] | None = None,
        is_avoid_query: bool | None = False,
    ):
        """Retrieves the top k results from the index for the given query.

        Args:
            query: A string representing the query.
            language: A string representing the language of the query.
            top_k: An integer representing the number of top results to retrieve.
            include_tags: A list of strings representing the tags to include in the results.
            exclude_tags: A list of strings representing the tags to exclude from the results.

        Returns:
            A list of dictionaries representing the retrieved results.
        """
        top_k = top_k or self.config.top_k
        language = language or self.config.language

        retrieval_engine = self.load_query_engine(
            top_k=top_k,
            language=language,
            include_tags=include_tags,
            exclude_tags=exclude_tags,
        )

        avoid_query = self.is_avoid_query or is_avoid_query

        query_bundle = QueryBundle(query_str=query)
        results = retrieval_engine.retrieve(
            query_bundle, is_avoid_query=bool(avoid_query)
        )

        outputs = [
            {
                "text": node.get_text(),
                "metadata": node.metadata,
                "score": node.get_score(),
            }
            for node in results
        ]
        self.is_avoid_query = None
        return outputs

    def __call__(self, query: str, **kwargs) -> List[Dict[str, Any]]:
        retrievals = self.retrieve(query, **kwargs)
        logger.debug(f"Retrieved {len(retrievals)} results.")
        logger.debug(f"Retrieval: {retrievals[0]}")
        return retrievals
