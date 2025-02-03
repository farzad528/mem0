import json
import logging
from typing import List, Optional

from pydantic import BaseModel

from mem0.vector_stores.base import VectorStoreBase

try:
    from azure.core.credentials import AzureKeyCredential
    from azure.core.exceptions import ResourceNotFoundError
    from azure.search.documents import SearchClient
    from azure.search.documents.indexes import SearchIndexClient
    from azure.search.documents.indexes.models import (
        BinaryQuantizationCompression,  # Added for binary quantization
    )
    from azure.search.documents.indexes.models import (
        HnswAlgorithmConfiguration,
        ScalarQuantizationCompression,
        SearchField,
        SearchFieldDataType,
        SearchIndex,
        SimpleField,
        VectorSearch,
        VectorSearchProfile,
    )
    from azure.search.documents.models import VectorizedQuery
except ImportError:
    raise ImportError(
        "The 'azure-search-documents' library is required. Please install it using 'pip install azure-search-documents==11.5.2'."
    )

logger = logging.getLogger(__name__)


class OutputData(BaseModel):
    id: Optional[str]
    score: Optional[float]
    payload: Optional[dict]


class AzureAISearch(VectorStoreBase):
    def __init__(
        self,
        service_name,
        collection_name,
        api_key,
        embedding_model_dims,
        compression_type="none",  # "none", "scalar", or "binary"
        use_float16=False,
    ):
        """
        Initialize the Azure AI Search vector store.

        Args:
            service_name (str): Azure AI Search service name.
            collection_name (str): Index name.
            api_key (str): API key for the Azure AI Search service.
            embedding_model_dims (int): Dimension of the embedding vector.
            compression_type (str): Specifies the type of quantization to use.
                Allowed values are "none", "scalar", or "binary".
            use_float16 (bool): Whether to store vectors in half precision (Edm.Half) or full precision (Edm.Single).
        """
        self.index_name = collection_name
        self.collection_name = collection_name
        self.embedding_model_dims = embedding_model_dims
        self.compression_type = compression_type.lower()
        self.use_float16 = use_float16

        self.search_client = SearchClient(
            endpoint=f"https://{service_name}.search.windows.net",
            index_name=self.index_name,
            credential=AzureKeyCredential(api_key),
        )
        self.index_client = SearchIndexClient(
            endpoint=f"https://{service_name}.search.windows.net",
            credential=AzureKeyCredential(api_key),
        )

        # Inject custom UserAgent header ("mem0") for tracking indexes created via mem0.
        try:
            self.search_client._client._config.user_agent_policy.add_user_agent("mem0")
            self.index_client._client._config.user_agent_policy.add_user_agent("mem0")
        except Exception as e:
            logger.warning(f"Failed to add custom UserAgent header: {e}")

        self.create_col()  # create the collection / index

    def create_col(self):
        """Create a new index in Azure AI Search."""
        # Determine vector type based on use_float16 setting.
        if self.use_float16:
            vector_type = "Collection(Edm.Half)"
        else:
            vector_type = "Collection(Edm.Single)"

        # Configure compression settings based on the specified compression_type.
        compression_configurations = []
        compression_name = None
        if self.compression_type == "scalar":
            compression_name = "myCompression"
            compression_configurations = [
                ScalarQuantizationCompression(compression_name=compression_name)
            ]
        elif self.compression_type == "binary":
            compression_name = "myCompression"
            compression_configurations = [
                BinaryQuantizationCompression(compression_name=compression_name)
            ]

        fields = [
            SimpleField(name="id", type=SearchFieldDataType.String, key=True),
            SimpleField(name="user_id", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="run_id", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="agent_id", type=SearchFieldDataType.String, filterable=True),
            SearchField(
                name="vector",
                type=vector_type,
                searchable=True, # Tehnically don't need this on SearchField but leaving for visibility
                vector_search_dimensions=self.embedding_model_dims,
                vector_search_profile_name="my-vector-config",
            ),
            SimpleField(name="payload", type=SearchFieldDataType.String, searchable=True),
        ]

        vector_search = VectorSearch(
            profiles=[
                VectorSearchProfile(name="my-vector-config", algorithm_configuration_name="my-algorithms-config")
            ],
            algorithms=[HnswAlgorithmConfiguration(name="my-algorithms-config")],
            compressions=compression_configurations,
        )
        index = SearchIndex(name=self.index_name, fields=fields, vector_search=vector_search)
        self.index_client.create_or_update_index(index)

    def _generate_document(self, vector, payload, id):
        document = {"id": id, "vector": vector, "payload": json.dumps(payload)}
        # Extract additional fields if they exist.
        for field in ["user_id", "run_id", "agent_id"]:
            if field in payload:
                document[field] = payload[field]
        return document

    # Note: Explicit "insert" calls may later be decoupled from memory management decisions.
    def insert(self, vectors, payloads=None, ids=None):
        """
        Insert vectors into the index.

        Args:
            vectors (List[List[float]]): List of vectors to insert.
            payloads (List[Dict], optional): List of payloads corresponding to vectors.
            ids (List[str], optional): List of IDs corresponding to vectors.
        """
        logger.info(f"Inserting {len(vectors)} vectors into index {self.index_name}")
        documents = [
            self._generate_document(vector, payload, id)
            for id, vector, payload in zip(ids, vectors, payloads)
        ]
        self.search_client.upload_documents(documents)

    def _build_filter_expression(self, filters):
        filter_conditions = []
        for key, value in filters.items():
            if isinstance(value, str):
                condition = f"{key} eq '{value}'"
            else:
                condition = f"{key} eq {value}"
            filter_conditions.append(condition)
        filter_expression = " and ".join(filter_conditions)
        return filter_expression

    def search(self, query, limit=5, filters=None, vector_filter_mode="preFilter"):
        """
        Search for similar vectors.

        Args:
            query (List[float]): Query vector.
            limit (int, optional): Number of results to return. Defaults to 5.
            filters (Dict, optional): Filters to apply to the search. Defaults to None.
            vector_filter_mode (str): Determines whether filters are applied before or after the vector search.
                Known values: "preFilter" (default) and "postFilter".

        Returns:
            list: Search results.
        """
        filter_expression = None
        if filters:
            filter_expression = self._build_filter_expression(filters)

        vector_query = VectorizedQuery(
            vector=query, k_nearest_neighbors=limit, fields="vector"
        )
        # Pass vector_filter_mode to the search call.
        search_results = self.search_client.search(
            vector_queries=[vector_query],
            filter=filter_expression,
            top=limit,
            vector_filter_mode=vector_filter_mode  # New query parameter for filter mode.
        )

        results = []
        for result in search_results:
            payload = json.loads(result["payload"])
            results.append(OutputData(id=result["id"], score=result["@search.score"], payload=payload))
        return results

    def delete(self, vector_id):
        """
        Delete a vector by ID.

        Args:
            vector_id (str): ID of the vector to delete.
        """
        self.search_client.delete_documents(documents=[{"id": vector_id}])
        logger.info(f"Deleted document with ID '{vector_id}' from index '{self.index_name}'.")

    def update(self, vector_id, vector=None, payload=None):
        """
        Update a vector and its payload.

        Args:
            vector_id (str): ID of the vector to update.
            vector (List[float], optional): Updated vector.
            payload (Dict, optional): Updated payload.
        """
        document = {"id": vector_id}
        if vector:
            document["vector"] = vector
        if payload:
            json_payload = json.dumps(payload)
            document["payload"] = json_payload
            for field in ["user_id", "run_id", "agent_id"]:
                document[field] = payload.get(field)
        self.search_client.merge_or_upload_documents(documents=[document])

    def get(self, vector_id) -> OutputData:
        """
        Retrieve a vector by ID.

        Args:
            vector_id (str): ID of the vector to retrieve.

        Returns:
            OutputData: Retrieved vector.
        """
        try:
            result = self.search_client.get_document(key=vector_id)
        except ResourceNotFoundError:
            return None
        return OutputData(id=result["id"], score=None, payload=json.loads(result["payload"]))

    def list_cols(self) -> List[str]:
        """
        List all collections (indexes).

        Returns:
            List[str]: List of index names.
        """
        indexes = self.index_client.list_indexes()
        return [index.name for index in indexes]

    def delete_col(self):
        """Delete the index."""
        self.index_client.delete_index(self.index_name)

    def col_info(self):
        """
        Get information about the index.

        Returns:
            Dict[str, Any]: Index information.
        """
        index = self.index_client.get_index(self.index_name)
        return {"name": index.name, "fields": index.fields}

    def list(self, filters=None, limit=100):
        """
        List all vectors in the index.

        Args:
            filters (Dict, optional): Filters to apply to the list.
            limit (int, optional): Number of vectors to return. Defaults to 100.

        Returns:
            List[OutputData]: List of vectors.
        """
        filter_expression = None
        if filters:
            filter_expression = self._build_filter_expression(filters)

        search_results = self.search_client.search(
            search_text="*",
            filter=filter_expression,
            top=limit
        )
        results = []
        for result in search_results:
            payload = json.loads(result["payload"])
            results.append(OutputData(id=result["id"], score=result["@search.score"], payload=payload))
        return results

    def __del__(self):
        """Close the search client when the object is deleted."""
        self.search_client.close()
        self.index_client.close()
