from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.sql import func
from database import Base
from sqlalchemy.orm import relationship


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(100), unique=True, nullable=False)
    nickname = Column(String(50), nullable=False)
    password = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Shelter(Base):
    __tablename__ = "shelters"

    id = Column(Integer, primary_key=True, index=True)
    desertion_no = Column(String(50), unique=True, nullable=True)
    name = Column(String(100), nullable=False)
    phone = Column(String(50), nullable=True)
    address = Column(String(255), nullable=True)
    website_url = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    dogs = relationship("Dog", back_populates="shelter")

class Dog(Base):
    __tablename__ = "dogs"

    id = Column(Integer, primary_key=True, index=True)
    shelter_id = Column(Integer, ForeignKey("shelters.id"), nullable=True)

    name = Column(String(50), nullable=False)
    breed = Column(String(50), nullable=True)
    age = Column(Integer, nullable=True)
    gender = Column(String(20), nullable=True)
    image_url = Column(String(255), nullable=True)

    description = Column(Text, nullable=True)
    personality_keywords = Column(Text, nullable=True)

    energy_level = Column(Integer, nullable=True)
    barking_level = Column(Integer, nullable=True)
    independence_level = Column(Integer, nullable=True)
    sociability_level = Column(Integer, nullable=True)
    walk_need = Column(Integer, nullable=True)

    good_with_children = Column(String(20), nullable=True)
    good_with_other_dogs = Column(String(20), nullable=True)
    separation_anxiety = Column(String(20), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    matching_results = relationship("MatchingResult", back_populates="dog")
    shelter = relationship("Shelter", back_populates="dogs")
    
class Survey(Base):
    __tablename__ = "surveys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    housing_type = Column(String(50), nullable=True)
    family_type = Column(String(50), nullable=True)
    outing_frequency = Column(String(50), nullable=True)
    daily_walk_time = Column(String(50), nullable=True)

    life_pattern = Column(String(50), nullable=True)
    walk_frequency = Column(String(50), nullable=True)
    walk_duration = Column(String(50), nullable=True)

    dog_experience = Column(String(50), nullable=True)
    mbti = Column(String(10), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class MatchingResult(Base):
    __tablename__ = "matching_results"

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    dog_id = Column(Integer, ForeignKey("dogs.id"), nullable=False)

    score = Column(Integer, nullable=False)
    reason = Column(Text, nullable=True)
    detail_analysis = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    dog = relationship("Dog", back_populates="matching_results")