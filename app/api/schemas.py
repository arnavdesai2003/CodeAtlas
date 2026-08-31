from pydantic import BaseModel, Field


class RepositoryCreateRequest(BaseModel):
    clone_url: str = Field(
        ...,
        min_length=1,
        examples=[
            "https://github.com/owner/repository.git"
        ],
    )


class RepositoryCreateResponse(BaseModel):
    repository_id: int
    name: str
    owner: str
    clone_url: str
    branch: str
    commit: str
    source_files_discovered: int
    local_path: str