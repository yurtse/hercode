from __future__ import annotations

import os
from datetime import datetime, timezone

import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .contracts import CreateRunRequest, TaskStatus


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repository: Mapped[str] = mapped_column(String(512))
    base_ref: Mapped[str] = mapped_column(String(128))
    objective: Mapped[str] = mapped_column(Text)
    acceptance_criteria: Mapped[list] = mapped_column(JSON)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default="planned")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    tasks: Mapped[list["Task"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    contract: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default=TaskStatus.PLANNED)
    branch: Mapped[str | None] = mapped_column(String(256), nullable=True)
    worktree: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    container_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    run: Mapped[Run] = relationship(back_populates="tasks")


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Store:
    def __init__(self, database_url: str | None = None):
        self.engine = create_engine(database_url or os.environ.get("DATABASE_URL", "sqlite:///factory.db"))
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def create_run(self, request: CreateRunRequest) -> Run:
        now = datetime.now(timezone.utc)
        run = Run(id=str(uuid.uuid4()), repository=request.repository, base_ref=request.base_ref,
                  objective=request.objective, acceptance_criteria=request.acceptance_criteria,
                  status="planned", created_at=now)
        with self.session_factory.begin() as session:
            session.add(run)
            for contract in request.task_dag:
                session.add(Task(id=contract.id, run_id=run.id, contract=contract.model_dump(mode="json"),
                                 status=TaskStatus.PLANNED, updated_at=now))
            session.add(Event(
                run_id=run.id,
                task_id=None,
                event="run_created",
                detail={"task_count": len(request.task_dag), "policy_profile": request.policy_profile},
                created_at=now,
            ))
        return run

    def get_run(self, run_id: str) -> Run | None:
        with self.session_factory() as session:
            return session.get(Run, run_id)

    def get_task(self, task_id: str) -> Task | None:
        with self.session_factory() as session:
            return session.get(Task, task_id)

    def tasks_for_run(self, run_id: str) -> list[Task]:
        with self.session_factory() as session:
            return list(session.scalars(select(Task).where(Task.run_id == run_id)).all())

    def running_tasks(self) -> list[Task]:
        with self.session_factory() as session:
            return list(session.scalars(select(Task).where(Task.status == TaskStatus.RUNNING)).all())

    def events_for_run(self, run_id: str, after_id: int = 0) -> list[Event]:
        with self.session_factory() as session:
            return list(session.scalars(
                select(Event).where(Event.run_id == run_id, Event.id > after_id).order_by(Event.id)
            ).all())

    def all_events(self) -> list[Event]:
        with self.session_factory() as session:
            return list(session.scalars(select(Event).order_by(Event.id)).all())

    def all_tasks(self) -> list[Task]:
        with self.session_factory() as session:
            return list(session.scalars(select(Task)).all())

    def record_event(self, run_id: str, event: str, detail: dict, task_id: str | None = None) -> Event:
        row = Event(run_id=run_id, task_id=task_id, event=event, detail=detail, created_at=datetime.now(timezone.utc))
        with self.session_factory.begin() as session:
            session.add(row)
        return row

    def approve(self, run_id: str) -> Run | None:
        now = datetime.now(timezone.utc)
        with self.session_factory.begin() as session:
            run = session.get(Run, run_id)
            if not run:
                return None
            run.approved, run.status = True, "approved"
            for task in session.scalars(select(Task).where(Task.run_id == run_id, Task.status == TaskStatus.PLANNED)):
                task.status = TaskStatus.READY
                task.updated_at = now
            session.add(Event(run_id=run_id, task_id=None, event="plan_approved", detail={}, created_at=now))
            return run

    def transition_task(self, task_id: str, status: TaskStatus, **values: object) -> Task:
        with self.session_factory.begin() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise KeyError(task_id)
            task.status = status
            task.updated_at = datetime.now(timezone.utc)
            for field, value in values.items():
                setattr(task, field, value)
            session.add(Event(run_id=task.run_id, task_id=task_id, event=f"task_{status}", detail=values, created_at=task.updated_at))
            states = list(session.scalars(select(Task.status).where(Task.run_id == task.run_id)).all())
            run = session.get(Run, task.run_id)
            if run:
                if all(value == TaskStatus.SUCCEEDED for value in states):
                    run.status = "succeeded"
                elif any(value == TaskStatus.RUNNING for value in states):
                    run.status = "running"
                elif any(value in {TaskStatus.FAILED, TaskStatus.BLOCKED} for value in states):
                    run.status = "blocked"
                elif all(value == TaskStatus.CANCELLED for value in states):
                    run.status = "cancelled"
            return task
